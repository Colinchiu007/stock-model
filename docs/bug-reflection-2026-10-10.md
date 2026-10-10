# 缺陷反思 2026-10-10：一个假门禁 + 一个静默成功的空组合

> 按质量节拍 **Bug 反思循环 5 步 SOP（QM-5）** 执行：根因溯源 → 逃逸链 → 系统性漏洞 → 修复+回归保护 → 预防措施。
> 两个发现都是「**不报错，但结果是错的**」——本项目 15 个历史缺陷的共同形态。

## 摘要

| # | 发现 | 性质 | 严重度 | 引入点 |
|---|------|------|--------|--------|
| A | **mypy 配了、装了、从不执行** —— 存量 86 个类型错误无人可见 | 流程/门禁缺陷 | 高 | `8097d59`（引入 CI 的提交） |
| B | **组合优化在收益率缺失时返回 200 + 空组合** —— 看着像优化成功 | 代码缺陷 | 高 | `c7feba9`（Phase 2 完成提交） |

两个都在同一次体检里被抓住，方式不同：
A 是**读配置**发现的（CI 里装了 mypy 却没有执行它的步骤），
B 是**补测试**发现的（我给改写过的分派逻辑补锁，测试当场变红）。

---

# 发现 A：假门禁 —— mypy 从诞生起就没跑过

## ① 第一性原因溯源

```
$ git log -S 'pip install ruff mypy' -- .github/workflows/ci.yml
8097d59 ci: add GitHub Actions CI and release workflows
```

该提交里的 Lint job：

```yaml
      - name: Install dev dependencies
        run: |
          pip install ruff mypy      # ← 装了 mypy
      - name: Ruff check
        run: ruff check src/ tests/
      - name: Ruff format check
        run: ruff format --check src/ tests/
                                     # ← 没有任何执行 mypy 的步骤
```

同时 `pyproject.toml` 认真配了：

```toml
[tool.mypy]
python_version = "3.10"
warn_return_any = true
warn_unused_configs = true
```

**意图是明确的**（装了依赖、写了配置），**执行环节缺失**。所以这不是"写错了"，而是
「**配置与执行之间没有连接**」——门禁以"依赖列表里有它"的形式存在，却从未运行。

为什么这段代码会写成这样：当时的关注点是"把 CI 跑起来"，ruff 立刻有输出、mypy 需要先
清存量才能接（80+ 错误），于是**先装后接**成了自然选择 —— 而"后"永远没到。

## ② 测试逃逸分析（按测试层级）

| 层级 | 结果 |
|------|------|
| 单元测试层 | **无测试**。没有任何用例检查 CI 配置内容 |
| 文档一致性层 | `tests/test_docs_consistency.py` 校验 README 与代码一致，但**不校验 CI 配置**；且它刻意只做"自洽校验"（历史教训），结构上不可能发现这类问题 |
| CI 自身层 | CI 只跑它自己被要求跑的东西 —— **门禁不会自我检查** |
| 代码审查层 | 单人仓库无 reviewer；且 diff 里"多了一行 pip install"看起来完全合理 |
| 本地命令层 | `docs/HANDOVER.md` 的命令速查只写了 `ruff check` / `ruff format`，**照文档做的人也不会跑 mypy** |

**逃逸原因分类：门禁缺失漏洞（伪装成"门禁存在"）**
—— 比"没有门禁"更危险：依赖列表、配置文件、文档副标题都暗示存在，
只有实际执行步骤里没有。这与项目历史上"界面显示运行中但没跑"是同一类。

## ③ 系统性漏洞定位

| 位置 | 具体缺陷 |
|------|---------|
| `.github/workflows/ci.yml` Lint job | `pip install ruff mypy` 与"无 mypy 执行步骤"并存。**"被安装"被当成了"被执行"** |
| `pyproject.toml` `[tool.mypy]` | 配置无任何消费者。加上 `warn_unused_configs = true` 本身也是反讽 —— mypy 连启动的机会都没有 |
| `docs/HANDOVER.md` 命令速查 | 提交前检查清单只列 ruff，把假门禁复制到了下一位接手者 |

**系统性缺陷分类：门禁缺失漏洞**。同类风险面：所有"配置/依赖已就位但执行步骤缺失"的门禁
（覆盖率阈值、安全扫描、契约测试、格式检查都属于此模式）。

## ④ 修复 + 回归保护测试

**修复**（三部分）：

1. `pyproject.toml` 增加 `[[tool.mypy.overrides]]`，**点名**豁免 11 个无类型信息/未安装的依赖。
   刻意不用全局 `ignore_missing_imports` —— 全局忽略会让"新引入无类型依赖"彻底不可见。
   分类依据是实测（包目录有无 `py.typed`）而非猜测：
   - 上游无类型信息：`akshare` `baostock` `plotly` `apscheduler` `scipy`
   - 有 stub 包但未安装（属后续优化）：`pandas` `yaml` `requests`
   - CI 不装的可选依赖：`gymnasium` `stable_baselines3` `pandas_ta`
2. 清掉存量 **86 个错误 → 0**：42 个由 overrides 消除（`import-untyped` 38 + `import-not-found` 4），
   44 个逐个修复（注解缺失 30 / 变量复用致推断错误 5 / 联合类型误报 2 / 其它 7）。
3. `ci.yml` Lint job 增加 mypy 执行步骤，**并补上依赖安装** ——
   缺依赖时 mypy 只会报一堆 "Cannot find implementation"，看着跑了其实没查。

**回归保护**：`tests/test_quality_gates.py`，锁的是「门禁存在且真的在跑」：

| 用例 | 锁住什么 |
|------|---------|
| `test_ci_actually_invokes_mypy` | CI 必须有执行 mypy 的 `run` 步骤（**变异验证：抽掉该步骤即红**） |
| `test_mypy_step_targets_real_source` | 调用必须指向 `src/`，不能查了个空目录 |
| `test_mypy_step_fails_the_build` | 不得用 `|| true` 吞掉失败 |
| `test_untyped_deps_are_exempted_explicitly` | 豁免必须点名，不能一刀切 |
| `test_no_global_ignore_missing_imports` | 禁止全局忽略（会掩盖新依赖） |

> 写这条锁时**第一次跑就撞上自己**：配置注释里为了解释"为什么不用它"而引用了
> `ignore_missing_imports` 这个词，测试把它当成了真配置 → 误报。
> 已改为先剥掉整行注释再做匹配。**注释里的承诺要 grep 验证，注释里的反例也一样。**

## ⑤ 预防措施（已落地）

1. `tests/test_quality_gates.py` 进入 CI —— 抽掉门禁步骤会立刻变红（已变异验证）
2. `pyproject.toml` 的 overrides 段写明分类依据与"点名而非全局"的理由，含后续优化方向
3. `docs/HANDOVER.md` 命令速查补上 `mypy src/stock_model`，并说明 CI 的 Lint job 现在跑三项
4. **教训入档**：本轮另一个坑（用 stub 模拟 CI 环境得到的数字与真实 CI 不一致）也写进 HANDOVER 5.2 ——
   **模拟环境能确认"不会失败"，不能用来写文档里的数字**

## ⑤-b 补充：门禁接入 CI 后立刻暴露的第三个坑

第一次推送后 **CI 的 Lint 挂了** —— 本地 mypy 是绿的，CI 多出 3 个错误：

```
src/stock_model/data/fetcher.py:215: error: Cannot assign to a method  [method-assign]
src/stock_model/data/fetcher.py:215: error: Incompatible types in assignment  [assignment]
src/stock_model/data/fetcher.py:216: error: "type[Session]" has no attribute "_trust_env_patched"  [attr-defined]
```

**根因：`ignore_missing_imports` 只豁免「缺少类型信息」，不豁免「类型信息在场时的真实错误」。**

- 本地 `requests` 不带 `py.typed` → `Session` 是 `Any` → monkey-patch 不报错
- CI 的 `requests 2.34.2` **自带 `py.typed`** → `Session` 是真类型 → 同一段代码被真检查

（CI 里其实**没有**装 `types-requests`；是同源问题的另一种表现：
**CI 装的是不带上限的最新依赖** —— 那次 CI 是 `pandas 3.0.6` / 本地 `2.3.3`。）

修复：`fetcher.py` 的 monkey-patch 是**有意为之**（禁系统代理，akshare 需要），
三个错误是补丁的必然结果而非缺陷，故按错误码精确豁免：

```python
requests.Session.__init__ = _patched_init  # type: ignore[method-assign, assignment]
requests.Session._trust_env_patched = True  # type: ignore[attr-defined]
```

选按错误码豁免而不是整段 `# type: ignore`：这样将来补丁引入**其它**类型问题时仍会报出来。
该写法对环境是稳健的 —— 本地没有 stub 时 ignore 用不上，而 `warn_unused_ignores` 未开启，不会误报。

**这条本身也进了预防措施**（HANDOVER 5.2）：本地 mypy 全绿 ≠ CI 全绿，
因为 CI 的依赖版本与本地不同。**mypy 门禁以 CI 为准。**

---

# 发现 B：组合优化返回 200 + 空组合

## ① 第一性原因溯源

```
$ git log --oneline -S 'if returns.empty' -- src/stock_model/portfolio/optimizer.py
c7feba9 feat: Phase 2 complete - quant engine, risk management, auto-collector,
        signal notifier, portfolio optimizer, RL agent, web dashboard (185 tests)
$ git blame -L 95,100 -- src/stock_model/portfolio/optimizer.py
c7feba9 2026-10-03 98)         if returns.empty:
c7feba9 2026-10-03 99)             return Portfolio(name="risk_parity")
```

同一模式在文件里共 **4 处**：`equal_weight:48`、`risk_parity:99`、`min_variance:168`、`mean_variance:274`。

**为什么当时会这么写**：意图是"空输入别崩"，于是返回一个空组合。看似防御性编程，
实际把「**输入不足**」和「**算完了但没权重**」变成了同一个返回值 —— 调用方无法区分。

第二层原因在端点：

```python
                if not prices_dict:
                    raise HTTPException(status_code=404, detail="无法获取数据")
                # ← 守卫只检查了"有没有价格"
                #   但 risk_parity / min_variance / mean_variance 真正需要的是"有没有收益率"
```

**守卫与前置条件错配**：同族方法签名不同（`equal_weight` 收 `symbols`，其余三个收 `returns`），
却共用一个守卫。

**实测后果**（标的只有 1 根 K 线时）：

```
GET /api/portfolio/optimize?symbols=000001&method=risk_parity
→ 200 {"method":"risk_parity","weights":{},"total_value":0.0}
```

不只是"空"——`total_value` 还从调用方要求的 100000 悄悄变成了 0
（因为 `Portfolio(name=...)` 用的是默认值）。前端会把它渲染成"优化完成，权重为空"。

## ② 测试逃逸分析（按测试层级）

| 层级 | 结果 |
|------|------|
| 单元测试层 `tests/test_batch3.py::TestPortfolioOptimizer` | 有 `test_risk_parity` / `test_min_variance` / `test_mean_variance`，但**全部传入合法 `sample_returns`**（100 行随机收益率）。空/退化输入从未被测 |
| 端点测试层 `tests/test_web_app.py::TestPortfolioAPI` | 覆盖 (a) 无数据 404、(b) `equal_weight` 成功。**三个需要收益率的方法从未在端点层被调用过** |
| 组合点 | 缺陷恰好要求「需收益率的方法 × 行情不足」这个**交叉组合**。两侧都有测试，交叉却没有 |
| 断言精度 | 端点用例只断言 `status_code == 200`；而 `equal_weight`（唯一被测的方法）不需要收益率，所以这个断言**足以通过**，掩盖了其它方法 |

**逃逸原因分类：覆盖组合缺失 + 断言不精确。**
两个测试各自都对，但都没站在缺陷所在的那个交叉点上。

## ③ 系统性漏洞定位

| 位置 | 具体缺陷 |
|------|---------|
| `optimizer.py:47/98/167/273` | 4 处 `if X.empty: return Portfolio(name=...)` —— 空结果与"成功的空结果"不可区分；且 `total_value` 静默丢失 |
| `src/stock_model/web/app.py` 组合优化端点 | 守卫写在 `prices_dict` 上，而三个分支的前置条件是 `returns_dict` |
| 分派结构 | 改写前用 `method_map + dict.get`，方法签名差异被藏在字典里，类型检查也看不见（mypy 报联合类型误报正是这个结构的副作用） |

**系统性缺陷分类：守卫与前置条件错配**。同类风险面：任何"按 method/type 分派到不同签名方法"的入口 ——
守卫必须按分支写，不能按共同的最弱条件写。

## ④ 修复 + 回归保护测试

**修复（两层，互为纵深）**：

1. **源头 fail-fast**：`PortfolioOptimizer._assert_has_returns()`，4 处静默早退改为抛 `ValueError`
   并给出可操作信息（"通常是行情历史不足，请拉长区间或改用 equal_weight"）。
   任何调用方都不会再拿到"空成功"。
2. **端点按分支守卫**：`RETURNS_BASED_METHODS` 常量 + 缺收益率时返回 **400**（不是 500、更不是 200），
   另外补 `except ValueError → 400`，避免参数问题混进服务端故障。

**回归保护**（4 个新锁，全部做过变异验证）：

| 用例 | 变异验证结果 |
|------|-------------|
| `test_empty_returns_raises_instead_of_empty_portfolio[3 个方法]` | 把 `_assert_has_returns` 改回 `return Portfolio(...)` → **红** ✅ |
| `test_equal_weight_empty_symbols_raises` | 同类（`n == 0` 早退） |
| `test_returns_based_method_without_history_is_rejected[3 个方法]` | 抽掉端点守卫 → **红** ✅ |
| `test_optimize_portfolio_dispatch[4 个方法]` + `..._unknown_method_falls_back` | 分派改造的行为等价锁 |

> 端点的守卫**独立锁**是变异验证时补上的：起初只断言 `status_code == 400` 与 `"收益率" in text`，
> 而优化器的报错同样满足这两条 —— 抽掉端点守卫测试照样绿，等于守卫没上锁。
> 补上"这句措辞只有端点守卫会产生"（`至少 2 根 K 线`）之后才真正锁住。

## ⑤ 预防措施（已落地）

1. 两条锁分工明确：单元锁锁源头 fail-fast，端点锁锁用户可见契约（含措辞）
2. 端点测试**刻意硬编码**三个方法名，不 import `RETURNS_BASED_METHODS` ——
   否则将来新增一个需要收益率的方法却忘了加守卫时，测试会跟着常量一起漂走
3. 分派改为显式分支：让 mypy 能逐个校验调用签名（这本来就是发现 B 的契机 ——
   为了修联合类型误报而补锁，才撞上真缺陷）
4. 本文件入档，并在 `docs/HANDOVER.md` 中登记

---

# 本轮数字

| 指标 | 修复前 | 修复后 |
|------|-------|--------|
| `mypy src/stock_model` | **86 errors / 31 files** | **Success (0), 57 files** |
| CI 是否执行 mypy | 否（装了但没跑） | 是（Lint job，缺依赖时亦失败） |
| 测试总数（本地装齐依赖） | 741 | **761**（760 passed + 1 skipped） |
| 其中质量门禁锁 | 0 | 8 |
| 覆盖率 | 85% | 85%（README 原写 81%，已修正） |
| 端到端重启验证 | 通过（见 HANDOVER 4.2） | 不变 |
