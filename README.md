# tensorfold-picture-cache

Patches and measurements for TensorFold (GLM-5.3-Flash-EXL3 on two DGX Sparks). All six fixes from the announcement live here:

| Fix (as announced) | Patch / where | Knob |
|---|---|---|
| 1. Copy, don't cut: resuming a shared prefix no longer cold-starts the donor session | [`0076`](patches/0076-glm-kept-state.patch) | `TF_GLM_POOL_COPY_RIVALS` (on by default) |
| 2. Kept-state memory capped at 4 GiB, freed blocks returned once 512 MB pile up | [`0076`](patches/0076-glm-kept-state.patch) | `TF_GLM_KEPT_BYTES_GIB` (default 4) |
| 3. A conversation keeps at most 2 of its own states | [`0076`](patches/0076-glm-kept-state.patch) | `TF_GLM_KEEP_PER_CHAT` (we run 2) |
| 4. Picture budget raised to 262144 (128 full-size images, no shrinking) | already stock in the v1.4 image — no patch needed | — |
| 5. A picture is read once, not on every turn (70 hot images 2.43 s → 0.59 s) | [`0074`](patches/0074-glm-picture-cache.patch) | `TENSORFOLD_GLM_PICTURE_CACHE=0` turns it off |
| 6. A marker the conversation quotes stays text (a session that carried 59 of them can send pictures again) | [`0075`](patches/0075-vision-quoted-markers.patch) | — |

A request carries the whole conversation, so the engine used to re-decode, re-fit and re-hash every picture in the history on every turn — that sits in front of the prompt and lands directly on time-to-first-token. Pixels never change, so the result of reading a picture is remembered by the source file's bytes: a later turn gets the row count and the key back and the file is not read again. Host memory only.

## Measurements (v1.4, two Sparks, 1920x1080 PNGs, engine prefill excluded)

### Baseline (unpatched, official image 5e01f1bb74d8)

| Case | Prompt tokens | Cold first token (engine prefill) | Hot first token (engine prefill) | Pictures |
|---|---|---|---|---|
| 10 images | 20,456 | 16.52 s (10.55 s) | 0.95 s (0.16 s) | 0.64 s |
| 70 images | 142,976 | 115.71 s (80.44 s) | 2.83 s (0.25 s) | 2.43 s |

≈ 29.8 ms per picture.

### Patched (same machine, same pictures, same questions)

| Case | Prompt tokens | Cold first token (engine prefill) | Hot first token (engine prefill) | Pictures |
|---|---|---|---|---|
| 10 images | 20,456 (matches baseline) | 17.35 s (10.67 s) | 0.69 s (0.16 s) | 0.37 s |
| 70 images | 142,976 (matches baseline) | 118.60 s (80.92 s) | 1.00 s (0.26 s) | 0.59 s |
| Text-only control, same token count | 101,118 | 53.05 s (52.79 s) | 0.38 s (0.18 s) | 0.04 s |

≈ 3.6 ms per picture (1/8 of before). The text-only control shows the remaining cost is not in the pictures.

## Correctness

- Same picture, same question, three times in a row (miss → hit → hit): 2,069 prompt tokens every time, identical answers.
- Prompt token counts match the baseline exactly (10 images 20,456 / 70 images 142,976): the patch changes no prompt arithmetic — rows, keys and fingerprints come from the same pixels, computed once.
- Cold rounds match the baseline: the miss path is untouched.
- Zero exceptions in the engine logs; prose decode 63.8 / 63.8 / 89.5 tok/s (acceptance 81–86%): the decode path is untouched.

## Overhead

Host memory only: the remembered source bytes are capped at 384 MB (`TENSORFOLD_GLM_PICTURE_CACHE_MB`, LRU), plus at most 8 fitted canvases (`TENSORFOLD_GLM_PICTURE_CANVASES`). Nothing is allocated on the GPUs.

## The kept-state pool: three fixes ([`patches/0076-glm-kept-state.patch`](patches/0076-glm-kept-state.patch))

All in `families/glm5_next/cuda/multi.py` (announced fixes 1/2/3):

- **Copy over cut.** Resuming a shared prefix used to take those pool rows over wholesale, cutting them off the donor and dropping every bookmark the donor had on top — the donor went cold from almost zero. The rows are now copied into an extent of the stream's own and the donor keeps its bookmarks (`TF_GLM_POOL_COPY_RIVALS=0` reverts to the cut).
- **A byte budget with hand-back.** The pool counted bookmark slots but never weighed what they own, and dropping one returned nothing: a few long conversations drove the hosts to 0.1 GB free. Kept states are now bounded together by bytes (`TF_GLM_KEPT_BYTES_GIB`, default 4 GiB, least-recently-used past that), and freed device memory is handed back to the driver once 512 MB has piled up (returning per drop measurably slowed decoding).
- **A per-conversation quota.** A bookmark after every model reply let one long conversation eat every slot, and conversations taking turns kept chilling each other out. A conversation now keeps a few of its own at its turn boundaries at most (`TF_GLM_KEEP_PER_CHAT`, we run 2; 0 is the old no-limit); the shared system-prompt state and mid-prefill pauses do not count against the quota.

`/health` reports the per-conversation state table (`kept_mix`) and the byte budget (`kept_bytes` / `kept_bytes_cap`). `patch -p0` onto a pristine v1.4 tree reproduces the deployed file byte for byte; `patch -p0 -R` walks it back.

## A marker the conversation quotes stays text ([`patches/0075-vision-quoted-markers.patch`](patches/0075-vision-quoted-markers.patch))

A conversation that once quotes a picture or clip marker — a pasted chat template, a log, this server's own refusal text — used to end every later turn that also carried a real picture:

```
400 the prompt's image and video markers do not match its images and videos (is <|​image|> or <|​video|> in the text?)
```

One real session carried **59 full image spans and 17 video spans** in its history. A quoted span and a template-written span are the same bytes, so [`0075`](patches/0075-vision-quoted-markers.patch) has `server/prompts.py`'s `MediaMarks` mark **every** picture and clip of a request with a per-request nonce rendered right before its marker, and adds a frontend `escape_quoted` that neutralizes every marker that does not stand directly behind one (a zero-width space after `<|`): a quotation stays text, the request's own markers still expand, and a template or client that drops a picture is still refused.

- Live: quoted template macro + 1 real image → 200 (was 400); the quotation inside a tool result → 200; 2 real images + quoted log → 200 with the correct first-image answer; prose decode 64.9 / 62.6 / 65.9 tok/s (floor 60 — prompt preparation only, decode untouched).
- Deployment/rollback: a third mounted file (`server/prompts.py`); `patch -p0 -R` returns byte-identically to the 0074 state and to pristine.

## Files

- Patch: [`patches/0074-glm-picture-cache.patch`](patches/0074-glm-picture-cache.patch) (`patch -p0` onto a pristine v1.4 tree reproduces the deployed file byte for byte; numbered 0074 because 0071–0073 are taken by other open PRs)
- Patch: [`patches/0075-vision-quoted-markers.patch`](patches/0075-vision-quoted-markers.patch) (applies after 0074; touches `tensorfold/server/prompts.py` and `tensorfold/vision/glm.py`)
- Patch: [`patches/0076-glm-kept-state.patch`](patches/0076-glm-kept-state.patch) (independent of the two above; touches `tensorfold/families/glm5_next/cuda/multi.py` only)
- Test scripts: [`pic-front-test2.py`](pic-front-test2.py) (10/70 images + text-only control), [`pic-correct.py`](pic-correct.py) (correctness)

## Upstream PRs

- PRs to MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold: [#63](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/63) (0074, picture frontend cache), [#64](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/64) (0075, quoted markers stay text), [#65](https://github.com/MiaAI-Lab/GLM-5.3-Flash-EXL3-2x-DGX-Sparks-TensorFold/pull/65) (0076, the kept-state pool).
