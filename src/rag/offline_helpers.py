"""
离线模式工具 —— 必须在所有第三方 import 之前 import 此模块。

问题：HuggingFaceEmbeddings / sentence_transformers 加载模型时，
huggingface_hub 会读取 HF_HUB_OFFLINE 常量以决定是否走网络。
但 huggingface_hub.constants 在模块导入时一次性读 env var，
之后任何 setdefault 都改变不了已缓存的常量。

解决：让本模块作为 entry script 的第一个 import（甚至先于 langchain），
直接覆盖式赋值（不用 setdefault），确保 huggingface_hub 看到的是 "1"。

本模块本身只依赖 stdlib（os），不会触发任何 langchain/huggingface_hub 链式加载。
"""
import os

# 直接赋值（不用 setdefault），强制覆盖任何"0"/空值
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
