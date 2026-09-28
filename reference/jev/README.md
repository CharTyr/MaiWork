# Jev Planner 前置处理器

MaiBot SDK2 插件。在 `maisaka.planner.before_request` 阶段调用 TypeSafe Jev，只处理 Planner 已组装好的输入。

## 行为边界

插件只做两件事：

1. 将宿主独立注入的 `【启发式记忆-内部参考】` 编号记忆逐条判断。删除需要**两个独立信号同时成立**：
   - `memory_N_relevant`（noul）低于 `memory_relevance_threshold`；
   - `memory_N_kind`（choice）被判为 `surface_overlap` 或 `unrelated`，且该标签概率 ≥ `memory_confirm_threshold`、`confidence` ≥ `memory_kind_min_confidence`。

   分类为 `same_entity`（同一主体，只是没提供所需背景）一律保留——这类是低相关里最容易被误删的。两个信号不一致时也保留。只有精确匹配宿主单-part格式的记忆块才会处理，候选超过配置上限时整块旁路。保留项按原始行输出，不重写、不重编号。

2. 当当前聊天包含依赖外部公开资料核验的客观事实要素时，向 Planner 的 System Item 追加一句客观属性标记；`fact_kind` 可信时带上固定词表里的类别：

```text
【Jev客观属性】当前聊天包含依赖外部公开资料核验的客观事实要素。
【Jev客观属性】当前聊天包含依赖外部公开资料核验的客观事实要素（类别：版本/发布）。
```

类别取自固定词表（版本/发布、价格/费用、日期/时效、公开人物或作品归属、技术规格），类别不可信或缺失时退回通用标记——**模型无法生成自由文本进入 Planner 提示词**。

插件明确不做：

- 不判断是否回复、回复谁或何时插话；
- 不生成回复建议，不指定措辞、语气、表达方向；
- 不修改 `tool_definitions`、`reply` 参数、Planner 输出或 Replyer 输入；
- 不发送任何群消息；
- 不接管 A_Memorix 的底层召回。

Jev 只返回 `noul` 概率与 `choice` 固定标签。具体删除/保留由插件中的固定阈值代码完成。

## 故障行为

默认关闭。启用后，API 无密钥、超时、429、响应缺字段或载荷无法解析时，当前 Planner 请求保持原样继续；不会阻断 MaiBot。

两道收敛措施：

- **同一窗口并发合并**：多个 Planner 轮次在同一个窗口上并发进入时，只发出一次 Jev 请求，其余等待同一结果（`joins`）。
- **熔断**：连续失败达到 `breaker_failure_threshold` 后，暂停调用 Jev `breaker_cooldown_s` 秒，期间全部原样放行；到期后放行一次探测，成功即恢复。
- 相同窗口按 SHA-256 缓存 `cache_ttl_s` 秒，避免同一 Planner 工具续轮重复请求。

## 观测

每处理 `stats_log_every_n_rounds` 轮向插件日志输出一行计数摘要（设为 0 关闭）：

```text
Jev 前置统计 rounds=120 no_state=9 cache_hit=57 joins=3 upstream_calls=41 upstream_ok=41
upstream_fail=0 timeout=0 breaker_open=0 breaker_skip=0 applied=38 unchanged=73
memories_dropped=12 blocks_dropped=1 memories_kept_same_entity=5
fact_markers=9 fact_categorized=7 latency_ms[n=41 p50=812 p95=1104 max=1902]
```

摘要只含计数与延迟，**不含任何聊天正文、记忆内容或凭据**。`memories_kept_same_entity` 是新增保守规则实际起作用的次数，`fact_categorized` 是带上类别的标记数。

## 配置

运行时配置为插件目录下 `config.toml`：

```toml
[plugin]
enabled = false
config_version = "0.3.0"
stats_log_every_n_rounds = 50

[preprocessor]
model = "jev-1.13.0"
key_file = "~/.typesafe_key"
timeout_s = 1.5
memory_relevance_threshold = 0.65
fact_signal_threshold = 0.70
memory_confirm_threshold = 0.60
memory_kind_min_confidence = 0.60
fact_kind_min_confidence = 0.60
breaker_failure_threshold = 3
breaker_cooldown_s = 60.0
```

密钥优先级：`TYPESAFE_API_KEY` 环境变量 → `api_key` 配置 → `TYPESAFE_KEY_FILE` / `key_file`。不要把密钥提交进版本库。

## 测试

核心逻辑（本地或服务器）从仓库根目录运行：

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

SDK2、Manifest 与生命周期硬门必须在 MaiBot 真实 venv 中运行；缺少宿主依赖会直接失败，不会跳过：

```bash
cd /opt/MaiBot
PYTHONPATH=/opt/MaiBot:/tmp/jev-preprocessor-stage \
  .venv/bin/python -m unittest discover -s /tmp/jev-preprocessor-stage/tests_host -v
```

两套测试共同覆盖：记忆逐条过滤、全块删除、保留字节不改写、候选超限整块旁路、用户粘贴 marker 不误删、最近窗口预算、客观属性标记、禁止回复指导词、输入不变性、缓存与并发合并、熔断与恢复、统计计数不含正文、网络/格式失败旁路、Hook 仅替换 `items`、当前 Manifest 校验、SDK2 工厂和生命周期契约。
