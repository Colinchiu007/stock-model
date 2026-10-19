# 缺陷反思 2026-10-19（补）：三源同时不可用与"测试污染"的连锁

> 本篇记录加新浪数据源过程中踩的两个坑。
> 第一个是产品缺陷（真故障），第二个是**我自己写的测试污染了全局**，
> 后者一度让 13 个无关用例变红 —— 比产品缺陷更值得警惕。

---

## 缺陷 A：两个数据源同时不可用 → 整个系统瘫痪

### 现象

2026-10-19 上午实测：

| 源 | 状态 |
|---|---|
| baostock | `10001011 黑名单用户` / `10002007 网络接收错误` |
| akshare（底层东方财富） | `RemoteDisconnected`（域名 TCP 通，但请求被对端断连） |

模拟盘无法推进、选股全失败。缓存也被我的冷缓存测试清空，无从兜底。

### 根因链（三层，逐层剥）

1. **表层**：降级链只有 2 个源，且是**二选一**结构
   （`_init_sources` 里 `fallback_name = "baostock" if source=="akshare" else "akshare"`）。
   两个都挂 → 无源可用。
2. **中层**：主源**构造失败会让整个 fetcher 构造崩**。
   `BaostockSource.__init__` 里就 `login()` 并直接抛 `ConnectionError`，
   于是 `StockDataFetcher(source="baostock")` 直接炸 —— 账户落盘
   `data_source=baostock` 时，**服务启动即挂**，连"降级"的机会都没有。
3. **底层**：新浪 `quotes.sina.cn` 实测可用（0.1s），但项目里根本没接。

### 修复

1. **接入新浪为第三源**（`SinaSource`）—— 实测约束直接决定实现：
   - 只能取"最近 N 根"（`datalen`），**没有区间参数** → 取回后本地切片
   - `datalen` 上限约 1000（请求 2000 返回非 JS 字面量，`literal_eval` 直接 SyntaxError）→ 硬封顶
   - `datalen` 按**自然日**估算而非交易日折算（折算会在起点落在长假前时少取，静默丢数据）
   - 不支持的复权/能力（估值/财务/板块）**显式抛异常**，不返回空表
     —— 返回空表会被误判为"成功但无数据"，反而掩盖问题
2. **降级链改成有序列表**：主源 → 逐个试备选，任一成功即返回
3. **主源构造失败不再阻断**：记为不可用，继续建降级链

### 附带修掉的契约问题

既有测试 `test_no_fallback_instance_primary_fails` 断言"无备选时抛原始异常类型"，
`test_get_daily_raises_real_error_not_retry_error` 靠"无备选源"观察主源异常。
它们锁的是**有意的旧行为**，不是我的改动写坏了代码 —— 但契约确实变了，
已更新并在注释里写明"要保护的东西没变"（用户看到的必须是解包后的真实异常）。

---

## 缺陷 B：测试污染全局状态（本篇重点）

### 现象

新写的 `tests/test_sina_source.py` 一旦运行，
后续 `test_strategy_engine.py` **13 个用例**、`test_web_app.py` 6 个、
`test_web_auth.py` 2 个接连变红（合计 31 个），
而这些文件**单独跑全绿**。

### 根因

```python
original = StockDataFetcher._create_source
StockDataFetcher._create_source = staticmethod(...)   # 替换
...
StockDataFetcher._create_source = original            # 还原 ← 这里出错
```

从类上取出来的是**裸函数**（`staticmethod` 描述符已被解包），
赋回去就成了普通实例方法，于是后续所有
`self._create_source(name)` 变成
`TypeError: takes 1 positional argument but 2 were given`。

即：**"还原"动作本身就是污染源**。

### 定位方法（值得复用）

失败在单独跑时消失、在全量跑时出现 ⇒ 跨测试污染。
二分法：把疑似文件与受害文件**两两同跑**，逐个缩小到最小组合
（`test_sina_source.py + test_strategy_engine.py` 即复现）。

### 修复

- 一律改用 `monkeypatch.setattr(cls, name, staticmethod(...))`
  —— 由 pytest 负责恢复，且不会剥掉描述符
- 能用 `object.__new__(Fetcher)` 直接装配的用例，根本不碰全局工厂
- 补注释说明**为什么不能手工赋值还原**，防止后人再犯

### 另一个同源问题

`create_app()` 里我加的启动预热线程在测试中被反复触发真联网，
一度导致 pytest 进程在收尾阶段崩溃。已用 `_is_pytest()` 门控：
预热是**部署期行为**，测试不需要它真的联网。

---

## 预防措施

| 措施 | 说明 |
|---|---|
| 改全局一律用 `monkeypatch` | 绝不手工赋值 + finally 还原（会剥描述符） |
| 污染排查走二分 | "单跑绿、全量红" = 跨测试污染，两两同跑缩小范围 |
| 后台线程要有测试门控 | 部署期行为（预热/定时）在测试里必须可关 |
| 不支持的能力显式抛异常 | 返回空表会被上层误判为"成功但无数据" |

---

## 验证

- 全量：**962 passed, 24 skipped, 0 failed**
  （对照：修复前 31 failed；干净 main 上也有 6 failed，属数据源/环境问题）
- 变异验证 **5/5 会红**：市场前缀判错 / datalen 不封顶 / 后复权放行 /
  降级链只试第一个 / 主源构造失败直接抛
- 端到端：主源 baostock 被拉黑时，自动降级取到 **673 行**（与 baostock 数据量一致）