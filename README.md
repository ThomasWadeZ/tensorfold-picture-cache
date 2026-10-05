# tensorfold-picture-cache

给 TensorFold（GLM-5.3-Flash-EXL3，双 DGX Spark）的补丁与实测记录。推文里的六个修复，补丁全在这里：

| 推文里的修复 | 补丁/位置 | 开关 |
|---|---|---|
| 1 复制代替抢走（新会话恢复共享前缀，不再把别的会话拖冷） | [`0076`](patches/0076-glm-kept-state.patch) | `TF_GLM_POOL_COPY_RIVALS`（默认开） |
| 2 存档内存 4 GiB 封顶 + 攒够 512 MB 归还驱动 | [`0076`](patches/0076-glm-kept-state.patch) | `TF_GLM_KEPT_BYTES_GIB`（默认 4） |
| 3 每个会话最多留 2 份自己的存档 | [`0076`](patches/0076-glm-kept-state.patch) | `TF_GLM_KEEP_PER_CHAT`（我们设 2） |
| 4 图片预算提到 262144（128 张全尺寸不缩） | v1.4 镜像已自带这个数字，无需补丁 | — |
| 5 同一张图只读一次（70 张热图 2.43s → 0.59s） | [`0074`](patches/0074-glm-picture-cache.patch) | `TENSORFOLD_GLM_PICTURE_CACHE=0` 可关 |
| 6 引用的标记不当图片（贴过模板/日志的会话也能带图） | [`0075`](patches/0075-vision-quoted-markers.patch) | — |

请求携带整条会话（含历史里的每一张图），引擎每轮都把每张图重新解码、缩放拟合、算指纹——这部分发生在引擎看到提示词之前，直接加在首字时间上。而图片的像素不会变，所以把"读一张图"的结果按源文件字节记下来：下一轮直接交回行数和 key，不再重读。只占主机内存。

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

## 会话存档的三个修复（[`patches/0076-glm-kept-state.patch`](patches/0076-glm-kept-state.patch)）

都在 `families/glm5_next/cuda/multi.py`，对应推文的修复 1/2/3：

- **复制代替抢走**：新会话从共享前缀恢复时，原来把那段缓存行"剪"走，连 donor 会话头上的存档一起扔掉，对方立刻全冷；现在复制一份自己接着写，别的会话一条缓存都不动（`TF_GLM_POOL_COPY_RIVALS=0` 退回剪走的行为）。
- **按字节封顶 + 归还**：原来只数存档"份"不称重，几条长会话能把主机内存吃到 0.1 GB；丢掉一份也从不还。现在所有存档合计 4 GiB 封顶（`TF_GLM_KEPT_BYTES_GIB`），超了按最久未用丢；释放的显存攒够 512 MB 才还给驱动（每丢一份就还，实测会拖慢解码）。
- **每会话限额**：原来每轮回复后自动存一份，一条长会话就能吃光所有槽位，几个会话轮流互相拖冷。现在每个会话在自己的回合边界最多留 2 份（`TF_GLM_KEEP_PER_CHAT`，0 是原来的不限）；共享系统提示的那份和预填中途的暂停不算额度。

`/health` 报每会话状态表（`kept_mix`）和字节预算（`kept_bytes` / `kept_bytes_cap`）。对 v1.4 纯净树 `patch -p0` 可逐字节复原部署文件，`patch -p0 -R` 可退回。

## 引用的标记不当图片（[`patches/0075-vision-quoted-markers.patch`](patches/0075-vision-quoted-markers.patch)）

对话里只要引用过引擎自己的模板源码、日志或报错原文（内含完整标记段），之后每一轮带图的请求都会被 400（`the prompt's image and video markers do not match its images and videos`）：引擎把"引用的标记"数成了真图片。一个真实会话实测躺着 59 个图片标记段 + 17 个视频标记段，从此带图必挂。引用的完整标记段与模板写的在字节上无法区分，所以 [`0075`](patches/0075-vision-quoted-markers.patch) 让 `server/prompts.py` 的 `MediaMarks` 给请求里**每一个**图片/视频打一次性标记（原来只在工具结果带图时打），前端新增 `escape_quoted` 把**不紧跟标记的**标记段在 `<|` 后插零宽空格转义：引用永远停留在文字里，请求自己的标记照常展开；裸 token（单独出现的 `<|​image|>`）同样转义。模板或客户端漏图时依旧拒绝，守卫没有放松。

- 实测：引用模板宏原文 + 1 张真图 200；引用塞在工具结果里 200；2 张真图 + 引用日志答"红色"（真标记仍落在正确位置）；散文解码 64.9 / 62.6 / 65.9 tok/s（底线 60；只动提示词准备，不碰解码路径）。
- 部署与回滚：第三个挂载文件（`server/prompts.py`），`patch -p0 -R` 逐字节退回 0074 状态与纯净树。

## 文件

- 补丁：[`patches/0074-glm-picture-cache.patch`](patches/0074-glm-picture-cache.patch)（对 v1.4 纯净树 `patch -p0` 可逐字节复原部署文件；编号 0074，因为 0071–0073 已被其他开放 PR 占用）
- 补丁：[`patches/0075-vision-quoted-markers.patch`](patches/0075-vision-quoted-markers.patch)（在 0074 之后 `patch -p0` 应用，改 `tensorfold/server/prompts.py` 与 `tensorfold/vision/glm.py` 两个文件）
- 补丁：[`patches/0076-glm-kept-state.patch`](patches/0076-glm-kept-state.patch)（独立于前两个，只改 `tensorfold/families/glm5_next/cuda/multi.py`）
- 复测脚本：[`pic-front-test2.py`](pic-front-test2.py)（10/70 张 + 纯文字对照）、[`pic-correct.py`](pic-correct.py)（正确性）

## 上游 PR

- 向上游 MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold 的 PR：[#63](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/63)（0074 图片前端缓存）、[#64](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/64)（0075 引用的标记不当图片）、[#65](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/65)（0076 会话存档三个修复）。
