# 虚拟机热换盘演练台（Flask + Docker Compose）

一个用于演练“虚拟磁盘热插拔 / 写入门暂停放行 / 落盘失效与故障注入 / 合批同步”的单机 Web 应用。
两份本地盘镜像（`disk_a.img`、`disk_b.img`，各 32 块 × 64 字节定长块），
一个槽位同一时刻只挂一份盘，运维可在网页上插拔换盘、读写定长块、暂停写入门、
排队放行写入、注入落盘失败，并观察并发写入如何**合用一次 fsync** 完成同步。

## 一、启动方式（Docker Compose）

```bash
docker compose up --build -d
# 浏览器打开
open http://localhost:8080      # macOS
# 或直接访问 http://<主机IP>:8080
```

- 镜像数据保存在命名卷 `disk_data`（容器内 `/data`），首次启动自动播种两份镜像。
- 健康检查：`GET /api/meta`。
- 重新播种（恢复两份镜像初始内容并重置全部状态）：网页右上角按钮，或
  `docker compose exec hotswap-disk python /app/scripts/seed.py`。
- 停止并**连同演练数据一起清空**：`docker compose down -v`。

本地无 Docker 时也可直接运行（需要 Python 3.11 + `pip install -r requirements.txt`）：

```bash
DISK_DATA_DIR=./data python -m waitress --host=0.0.0.0 --port=8080 --threads=8 wsgi:application
```

## 二、核心机制：挂载代次（mount generation）

- 每次拔盘 / 插盘 / 换盘，槽位 `mount_generation` 都 +1，即使插回的是同一块盘。
- 写入在**发起时**捕获当前代次；真正落盘前（拿到块锁之后还要二次核对）比对代次：
  代次不一致或盘 ID 不一致 → 判 `StaleWriteError`，拒绝写文件。
- 因此“写入落盘前暂停 → 换盘 → 放行”的旧写入必然失效，**任何一份镜像都不会被改动**，
  插回原盘也一样失效。
- 每个(盘, 块)有独立锁：不同块的写入真正并行；同块写入串行、整块落盘，不会撕裂。
  锁按 **(盘ID, 块号)** 区分，旧盘的批次正在等同步时换上新盘，新盘同号块用的是另一把锁，读写不被阻塞。
- 模拟落盘失败在真正 `open/write` 之前抛错，原块内容完整保留。

## 二之二、合批同步：多条写入合用一次 fsync

改造前每条直接写入都各自 `flush + fsync`，两块并发写就要同步两次。现在的流程：

1. 写入通过失效/故障预检、拿到自己的 (盘,块) 锁后，进入按 **(盘ID, 挂载代次)** 归属的同步批次。
2. 第一个到达的写入作为 leader 建批，并打开一个很短的合流窗口
   （`BATCH_JOIN_WINDOW`，默认 25ms，可用环境变量覆盖）；**同盘、同挂载代次、写不同块**
   的就绪写入在窗口内并入同一批次。同一块因块锁互斥，只能进后续批次。
3. 窗口关闭后 leader 依次 `seek/write` 批次内各块，再调用**唯一一次** `flush + fsync`；
   批次成员在自己的块锁上等待，同步成功后才把状态置为 `done` 并返回。
4. 只有一条写入时照常建批落盘（单成员批次 = 一次 fsync，行为与改造前等价）。
5. 参与批次的块在同步结束前一直持有块锁，因此**回读必须等同步完成**，不会读到
   “已 write 尚未 fsync”的中间视图；写请求也只有同步成功才报落盘。
6. 批次一旦开始就只写它所属的那份镜像：等同步期间换盘，批次仍在原盘完成；
   旧挂载代次的其它写入在二次核对处判 `StaleWriteError`，不会混进新盘的批次。
7. 故障开关在真正动盘前检查：开关打开时，**尚未动盘的批次整批报 `SimulatedFlushError`**，
   不产生任何 fsync，所有成员的原块保持不变。

状态接口 `/api/state` 新增 `fsync_total`（进程内累计同步次数）与 `batches`
（近期批次摘要：批次号、盘、代次、成员块、成员数、成败）；每条写入结果带 `batch_id`。
写入接口新增可选字段 `sync_delay`（秒）：让所属批次在 fsync 前睡眠，便于演练
“同步进行中换盘”。

## 三、HTTP API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全量状态（槽位、门、队列、日志、盘清单） |
| GET | `/api/meta` | 块大小 / 块数量 |
| GET | `/api/blocks/<n>` | 读取当前挂载盘的第 n 块 |
| POST | `/api/blocks/<n>/write` | 定长块写入；body `{payload, delay, sync_delay, note}`，门暂停时自动排队 |
| POST | `/api/slot/plug` | `{disk_id}` 挂载/换盘（代次+1） |
| POST | `/api/slot/unplug` | 拔盘置空（代次+1） |
| POST | `/api/gate` | `{open: true/false}` 放行 / 暂停写入门 |
| POST | `/api/fail-simulation` | `{enabled: true/false}` 故障注入开关 |
| POST | `/api/pending/<id>/release` | 放行单条排队写入 |
| POST | `/api/pending/release-all` | 全部放行，返回每条 done/stale/failed |
| POST | `/api/pending/<id>/discard` | 丢弃排队写入 |
| POST | `/api/reseed` | 重新播种镜像并重置 |

错误约定：业务错误 HTTP 409（`StaleWriteError` 等），模拟落盘失败 HTTP 500
（`SimulatedFlushError`），响应体均为 `{ok:false,type,error}`。

## 四、网页推荐演练路径

1. **慢写换盘**：门保持放行，块 0 写入延时填 5 秒发起；请求发出后立刻换到另一块盘。
   请求返回“写入失效”，事件日志与镜像哈希均可证明镜像未变。
2. **暂停读写新盘**：切到暂停，对当前盘写入（入队）；换盘后可读新盘、对新盘写入（也入队）；
   全部放行后旧写入 stale、新写入 done。
3. **插回原盘**：排队后换盘再换回来，放行旧写入仍报失效。
4. **落盘失败**：开启故障注入后写入，报 500；重读该块仍是原内容；关闭后写入恢复。
   故障开启时并发两条写入，会看到整批 500、`fsync_total` 不增长。
5. **并发写入合批**：用第⑤区对两个不同块同时写入，页面显示两条写入同属一个批次、
   **合用 1 次 fsync**，并在同步成功后回读两块，判定两处改动是否都保留。
6. **同步中换盘**：第⑤区填“同步前延时”3 秒后并发写入，等待期间换到另一块盘：
   换盘立即生效，新盘同号块可立即读写（不被旧同步阻塞）；旧批次仍只在原盘完成，
   仍在等批次的旧代次写入报失效。

## 五、测试

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v          # 36 个单元/HTTP 测试（含 10 个合批同步专项）
python -I scripts/e2e_drill.py      # 30 项端到端演练（需先启动服务，纯标准库）
```

## 六、目录结构

```
app/
  __init__.py          # Flask 路由与错误处理
  disk_manager.py      # 核心引擎：槽位代次、(盘,块)锁、合批 fsync、排队、失效、故障注入
  static/              # 单页前端（原生 HTML/CSS/JS，无构建依赖）
scripts/
  seed.py              # 重新播种镜像
  e2e_drill.py         # 端到端演练脚本
tests/                 # pytest 套件
Dockerfile, docker-compose.yml, requirements.txt, wsgi.py
```
