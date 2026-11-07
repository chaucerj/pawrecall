# LoCoMo 基准

用公开长对话数据集 [LoCoMo](https://huggingface.co/datasets/snap-research/locomo) 度量
pawrecall 的检索质量，产出可与 Hy-Memory / Mem0 / MemOS 等对比的公开数字。

## 指标

- **evidence recall@k / MRR**：检索结果是否命中标注证据（文本重叠判定，零 LLM 成本，可复现）
- **answer accuracy（--judge）**：用 chat 模型判「检索内容能否答对题」——
  事实层（蒸馏改写）的增益在这个指标上才可见

## 用法

```bash
# 1. 拿数据（二选一）
python3 bench/bench_locomo.py --hf                     # 直接下载
# 或手动下载 locomo10.json 后：
python3 bench/bench_locomo.py --data locomo10.json

# 2. 基础检索评测（默认 auto 模式，类别 1-4）
python3 bench/bench_locomo.py --data locomo10.json --mode exact   # 对比各模式
python3 bench/bench_locomo.py --data locomo10.json --mode semantic
python3 bench/bench_locomo.py --data locomo10.json --mode facts   # 仅事实层

# 3. 带判答（需配置 facts.json 的 chat 模型）
python3 bench/bench_locomo.py --data locomo10.json --judge
```

基准全程用独立副本库 `~/.pawrecall/bench_locomo.db`，不碰生产库；换模式重跑
不必重建（`--rebuild` 可强制重建）。

## 自检

```bash
python3 bench/bench_locomo.py --selftest   # 离线合成数据，验证 harness 本身
```
