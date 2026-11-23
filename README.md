# 虚拟机热换盘演练台（Flask + Docker Compose）

一个用于演练“虚拟磁盘热插拔 / 写入门暂停放行 / 落盘失效与故障注入”的单机 Web 应用。
两份本地盘镜像（`disk_a.img`、`disk_b.img`，各 32 块 × 64 字节定长块），
一个槽位同一时刻只挂一份盘，运维可在网页上插拔换盘、读写定长块、暂停写入门、
排队放行写入，并注入落盘失败。

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
- 每个块有独立锁：不同块的写入真正并行；同块写入串行、整块落盘，不会撕裂。
- 模拟落盘失败在真正 `open/write` 之前抛错，原块内容完整保留。

## 三、HTTP API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 全量状态（槽位、门、队列、日志、盘清单） |
| GET | `/api/meta` | 块大小 / 块数量 |
| GET | `/api/blocks/<n>` | 读取当前挂载盘的第 n 块 |
| POST | `/api/blocks/<n>/write` | 定长块写入；body `{payload, delay, note}`，门暂停时自动排队 |
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
5. **并发写入**：用第⑤区对两个不同块同时写入，页面自动回读并判定两处改动是否都保留。

## 五、测试

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v          # 26 个单元/HTTP 测试
python -I scripts/e2e_drill.py      # 17 项端到端演练（需先启动服务，纯标准库）
```

## 六、目录结构

```
app/
  __init__.py          # Flask 路由与错误处理
  disk_manager.py      # 核心引擎：槽位代次、块锁、排队、失效、故障注入
  static/              # 单页前端（原生 HTML/CSS/JS，无构建依赖）
scripts/
  seed.py              # 重新播种镜像
  e2e_drill.py         # 端到端演练脚本
tests/                 # pytest 套件
Dockerfile, docker-compose.yml, requirements.txt, wsgi.py
```
