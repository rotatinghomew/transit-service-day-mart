# 联运服务日事实集市

接收城际接驳的运营日、停靠时刻、临时服务指令及步行换乘通道资料，按**运营日**
形成票务与站内显示可复用的数据。`data/facts.json` 为本地合成排班与临时调整资料。

需要 Python 3.11 或更新版本，无第三方依赖。

## 能做什么

* **运营日归属**：按每个运营方自己的运营日历（时区 + 每日起算时刻）把跨零点班次
  归入同一运营日。同一班次跨日期、本地时间重复/缺失且资料不足以判定的，进入隔离区。
* **临时指令**：支持增开、取消整趟、取消/替代/改时刻/增停单个停靠。指令可按适用
  日期或明确时刻范围定位；迟交指令若被后续适用指令明确取代，不会把已生效改线复原。
* **票面站码与实际停靠**：保留票面原站码并在其适用期内解释；换乘判定一律用两班车
  的**实际停靠点**、可走通道与到发间隔，不用票面旧码推断换乘。
* **通道与等待规则**：支持每日开放时段、日期范围与末段通行后的等待分钟数；施工
  只开半天时按旅客实际可通行时间判定。
* **确认范围**：停靠、通道或改线未确认时，连接只作「待定」给站内人员看，`sellable`
  为 `false`，绝不当作可售连接。
* **交割与重取**：同一输入重复构建得到相同版本号；`DeliveryStore` 支持按运营日
  存档、重取与更正交割，并给出撤行/保留/新增清单，避免「半张表已换站、另一半还
  在卖旧换乘」。
* **事后核对**：`audit` 同时返回交割时版本与当前更正结果，`compare` 说明连接因
  停靠、路径、确认范围还是时刻而变。

## 快速开始

```python
from datetime import date
from serviceday import DeliveryStore, build_day, load_facts

facts = load_facts("data/facts.json")
board = build_day(facts, date(2026, 11, 29))
print(board.version)
for stop in board.stops:
    print(stop.trip_id, stop.official_stop_code, stop.ticket_stop_code, stop.actual_place)
for connection in board.connections:
    print(connection.from_trip, "->", connection.to_trip, connection.state, connection.sellable)

store = DeliveryStore()
delivered = store.deliver(facts, date(2026, 11, 29))
manifest = store.deliver_correction(facts, date(2026, 11, 29))
```

命令行：

```sh
python3 -m serviceday.day --day 2026-11-29 --facts data/facts.json
python3 -m serviceday.day --day 2026-11-29 --facts data/facts.json --operator A --audit
```

## 输出结构

| 字段 | 含义 |
| --- | --- |
| `service_day` | 运营日（不是自然日） |
| `version` | 内容与依据的稳定摘要，重取一致 |
| `stops` | 该日实际停靠：`official_stop_code`（实际停靠站码）、`ticket_stop_code`（票面原站码）、`actual_place`、到发绝对时刻、是否 `provisional` |
| `connections` | 每对服务一个判定：`available` / `pending` / `unavailable`；`sellable` 仅 `available` 为真 |
| `isolated` | 因时刻歧义、站码缺失或指令冲突被隔离的服务及缘由 |
| `evidence` | 生效指令、运营日历与所用通道资料 |

## 测试

```sh
python3 -m unittest discover -s tests
```

覆盖运营日归属与跨零点、夏令时重复/缺失、指令取代链、票面站码解释、通道开放
时段与等待规则、连接判定、隔离、版本重取一致性与命令行输出。
