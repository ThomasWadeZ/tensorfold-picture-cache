# tensorfold-picture-cache

给 TensorFold（GLM-5.3-Flash-EXL3，双 DGX Spark）的补丁与实测记录：图片前端缓存 [`0074`](patches/0074-glm-picture-cache.patch)、引用的标记不当图片 [`0075`](patches/0075-vision-quoted-markers.patch)。

请求携带整条会话（含历史里的每一张图），引擎每轮都把每张图重新解码、缩放拟合、算指纹——这部分发生在引擎看到提示词之前，直接加在首字时间上。而图片的像素不会变，所以把"读一张图"的结果按源文件字节记下来：下一轮直接交回行数和 key，不再重读。只占主机内存；`TENSORFOLD_GLM_PICTURE_CACHE=0` 关闭，`..._MB` 限制记住的源字节。

## 实测（v1.4，两台 Spark，1920x1080 PNG，引擎 prefill 已刨除）

### 基线（未打补丁，官方镜像 5e01f1bb74d8）

| 用例 | 提示 token | 冷轮首字（引擎预填） | 热轮首字（引擎预填） | 图片部分 |
|---|---|---|---|---|
| 10 张图 | 20,456 | 16.52s（10.55s） | 0.95s（0.16s） | 0.64s |
| 70 张图 | 142,976 | 115.71s（80.44s） | 2.83s（0.25s） | 2.43s |

每张图 ≈ 29.8 ms。

### 打补丁后（同一台机器、同一批图、同样的问法）

| 用例 | 提示 token | 冷轮首字（引擎预填） | 热轮首字（引擎预填） | 图片部分 |
|---|---|---|---|---|
| 10 张图 | 20,456（与基线一致） | 17.35s（10.67s） | 0.69s（0.16s） | 0.37s |
| 70 张图 | 142,976（与基线一致） | 118.60s（80.92s） | 1.00s（0.26s） | 0.59s |
| 同 token 数纯文字对照 | 101,118 | 53.05s（52.79s） | 0.38s（0.18s） | 0.04s |

每张图 ≈ 3.6 ms（原来的 1/8）。纯文字对照说明剩下的开销不在图片上。

## 正确性

- 同一张图、同一个问题连发三次（第 1 次未命中走解码，第 2/3 次命中走缓存）：提示 token 数三次都是 2069，答案逐字相同。
- 提示 token 数与基线完全一致（10 张 20,456 / 70 张 142,976）：补丁不改任何提示词算术，行数、key、指纹都从同样的像素算出，只是只算一次。
- 冷轮（未命中）与基线基本一致：未命中路径行为未变。
- 引擎日志零异常；散文解码 63.8 / 63.8 / 89.5 tok/s（验收 81–86%）：解码路径未被触碰。

## 开销

只占主机内存：记住的源字节上限 384 MB（`TENSORFOLD_GLM_PICTURE_CACHE_MB`，LRU），另外最多留 8 张拟合好的画布（`TENSORFOLD_GLM_PICTURE_CANVASES`）。显卡上不分配任何东西。

## 引用的标记不当图片（[`patches/0075-vision-quoted-markers.patch`](patches/0075-vision-quoted-markers.patch)）

对话里只要引用过引擎自己的模板源码、日志或报错原文（内含完整标记段），之后每一轮带图的请求都会被 400（`the prompt's image and video markers do not match its images and videos`）：引擎把"引用的标记"数成了真图片。一个真实会话实测躺着 59 个图片标记段 + 17 个视频标记段，从此带图必挂。引用的完整标记段与模板写的在字节上无法区分，所以 [`0075`](patches/0075-vision-quoted-markers.patch) 让 `server/prompts.py` 的 `MediaMarks` 给请求里**每一个**图片/视频打一次性标记（原来只在工具结果带图时打），前端新增 `escape_quoted` 把**不紧跟标记的**标记段在 `<|` 后插零宽空格转义：引用永远停留在文字里，请求自己的标记照常展开；裸 token（单独出现的 `<|image|>`）同样转义。模板或客户端漏图时依旧拒绝，守卫没有放松。

- 实测：引用模板宏原文 + 1 张真图 200；引用塞在工具结果里 200；2 张真图 + 引用日志答"红色"（真标记仍落在正确位置）；散文解码 64.9 / 62.6 / 65.9 tok/s（底线 60；只动提示词准备，不碰解码路径）。
- 部署与回滚：第三个挂载文件（`server/prompts.py`），`patch -p0 -R` 逐字节退回 0074 状态与纯净树。

## 文件

- 补丁：[`patches/0074-glm-picture-cache.patch`](patches/0074-glm-picture-cache.patch)（对 v1.4 纯净树 `patch -p0` 可逐字节复原部署文件；编号 0074，因为 0071–0073 已被其他开放 PR 占用）
- 补丁：[`patches/0075-vision-quoted-markers.patch`](patches/0075-vision-quoted-markers.patch)（在 0074 之后 `patch -p0` 应用，改 `tensorfold/server/prompts.py` 与 `tensorfold/vision/glm.py` 两个文件）
- 复测脚本：[`pic-front-test2.py`](pic-front-test2.py)（10/70 张 + 纯文字对照）、[`pic-correct.py`](pic-correct.py)（正确性）

## 上游 PR

- 向上游 MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold 的 PR：[#63](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/63)（0074 图片前端缓存）、[#64](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/64)（0075 引用的标记不当图片）
