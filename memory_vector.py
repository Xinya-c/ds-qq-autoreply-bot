#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""memory_vector.py —— 本地向量检索（记忆的可选增强层）。

模型：bge-small-zh-v1.5（ONNX q8，约 23MB，512 维，中文检索向）。
首次使用自动下载（hf-mirror 镜像优先，失败回退 huggingface.co），
之后**运行时零联网、不调任何云端 API**。

依赖缺失 / 模型不在位 / 下载失败时全部优雅降级：`available()` 返回
False，上层（memory.py）自动回退到关键词检索——本模块的任何异常都
不许影响主流程。

存储：`<归档目录>/vectors.npy`（N×512 float32），与关键词索引的
record 顺序**严格对齐**；它是缓存不是真相，文件丢了重启时按归档重建。

依赖（均为可选，缺失即降级）：
    pip install onnxruntime tokenizers numpy
"""

import os
import shutil
import threading
import urllib.request

# 标定记录（2026-10-24，bge-small-zh-v1.5，真实归档 5166 条）：
#   查询侧**不加** bge 指令前缀——实测 6/6 查询对去前缀后分数全面更高
#   （0.69 vs 0.53），前缀对短闲聊查询是拖累
#   无关配对相似度 p50=0.41 / p90=0.54 → 绝对底线取 0.52
MAX_LEN = 128                     # 聊天行很短，128 token 足够，快
DIM = 512

MODEL_NAME = "bge-small-zh-v1.5"
_BASES = (
    "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/",
    "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/",
)
_FILES = ("onnx/model_quantized.onnx", "tokenizer.json")


def _log(msg):
    try:
        from memory import log
        log(msg)
    except Exception:
        try:
            print(msg, flush=True)
        except Exception:
            pass


def deps_ok():
    """三个依赖是否都能导入。"""
    try:
        import onnxruntime      # noqa: F401
        import tokenizers       # noqa: F401
        import numpy            # noqa: F401
        return True
    except Exception:
        return False


def model_dir(base_dir):
    return os.path.join(base_dir, "models", MODEL_NAME)


def ensure_model(base_dir, timeout=180):
    """确保模型文件在位（不在则下载）。返回模型目录或 None。"""
    d = model_dir(base_dir)
    try:
        if all(os.path.exists(os.path.join(d, f)) for f in _FILES):
            return d
        os.makedirs(d, exist_ok=True)
        for rel in _FILES:
            dst = os.path.join(d, rel)
            if os.path.exists(dst):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            ok = False
            for base in _BASES:
                url = base + rel
                try:
                    _log("  下载模型文件 %s（%s）…" % (rel, base.split("/")[2]))
                    req = urllib.request.Request(
                        url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=timeout) as r, \
                            open(dst + ".part", "wb") as f:
                        shutil.copyfileobj(r, f)
                    os.replace(dst + ".part", dst)
                    ok = True
                    break
                except Exception as e:
                    _log("    下载失败（%s）：%s" % (base.split("/")[2], e))
                    try:
                        os.remove(dst + ".part")
                    except OSError:
                        pass
            if not ok:
                return None
        return d
    except Exception as e:
        _log("模型准备失败: %s" % e)
        return None


class Embedder:
    """bge-small-zh 本地编码器（CLS pooling + L2 归一化）。"""

    def __init__(self, base_dir):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        d = ensure_model(base_dir)
        if d is None:
            raise RuntimeError("模型不在位")
        self.tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
        self.tok.enable_truncation(max_length=MAX_LEN)
        self.tok.enable_padding(length=MAX_LEN)
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.sess = ort.InferenceSession(
            os.path.join(d, "onnx", "model_quantized.onnx"),
            sess_options=so, providers=["CPUExecutionProvider"])

    def encode(self, texts, is_query=False):
        """编码一批文本 → (n, 512) 已归一化。

        is_query 参数保留仅为接口兼容：标定发现查询侧不加前缀效果更好，
        因此两侧都用同样编码（见模块顶部标定记录）。
        """
        encs = self.tok.encode_batch(texts)
        np = self.np
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        tids = np.array([e.type_ids for e in encs], dtype=np.int64)
        out = self.sess.run(
            None, {"input_ids": ids, "attention_mask": mask,
                   "token_type_ids": tids})
        cls = out[0][:, 0]                      # CLS pooling
        norm = np.linalg.norm(cls, axis=1, keepdims=True)
        norm[norm == 0] = 1.0
        return cls / norm


class VectorStore:
    """与关键词索引 record 顺序对齐的向量缓存。

    - load(path)：存在且形状合法则载入
    - extend(embeds)：内存追加（调用方负责与 record 同步追加）
    - search(qvec, k)：余弦 top-k，返回 [(score, rec_idx)]
    - flush()：落盘（脏标记由调用方管理，全量重写，几 MB 不心疼）
    """

    def __init__(self, path):
        self.path = path
        self.vecs = None
        self._lock = threading.Lock()

    def load(self):
        try:
            import numpy as np
            if os.path.exists(self.path):
                a = np.load(self.path)
                if a.ndim == 2 and a.shape[1] == DIM:
                    self.vecs = a
                    return len(a)
        except Exception as e:
            _log("向量缓存加载失败（将重建）: %s" % e)
        self.vecs = None
        return 0

    def __len__(self):
        return 0 if self.vecs is None else len(self.vecs)

    def aligned(self, n):
        return self.vecs is not None and len(self.vecs) == n

    def extend(self, embeds):
        import numpy as np
        with self._lock:
            if self.vecs is None:
                self.vecs = np.asarray(embeds, dtype=np.float32)
            else:
                self.vecs = np.vstack([self.vecs,
                                       np.asarray(embeds, dtype=np.float32)])

    def set_all(self, embeds):
        import numpy as np
        with self._lock:
            self.vecs = np.asarray(embeds, dtype=np.float32)

    def flush(self):
        try:
            import numpy as np
            with self._lock:
                if self.vecs is None:
                    return
                # np.save 会给不带 .npy 的名字补后缀，tmp 必须自带 .npy
                tmp = self.path + ".tmp.npy"
                np.save(tmp, self.vecs)
                os.replace(tmp, self.path)
        except Exception as e:
            _log("向量缓存落盘失败: %s" % e)

    def search(self, qvec, k=8, skip=None):
        """余弦 top-k（跳过 skip 集合里的下标）。"""
        import numpy as np
        if self.vecs is None or len(self.vecs) == 0:
            return []
        with self._lock:
            sims = self.vecs @ qvec[0]           # 已归一化，点积即余弦
        order = np.argsort(-sims)
        out = []
        for i in order:
            i = int(i)
            if skip and i in skip:
                continue
            out.append((float(sims[i]), i))
            if len(out) >= k:
                break
        return out
