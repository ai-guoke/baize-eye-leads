# 归档：一次性探测 / 实验室脚本

开发过程中用过的临时脚本，**不是现网运行依赖**。内网交付不需要它们。

| 目录 | 内容 |
|------|------|
| `probes/` | 根目录 `_probe_*` / `_explore` / `_check_env` / `_test_q_fields` 等 |
| `lab/` | `qcc_lab` 建模期脚本（实验室库已拆除） |

需要翻旧结论时再打开；日常启动只用：

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```
