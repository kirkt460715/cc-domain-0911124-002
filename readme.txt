# 机队观测接入

管理机队设备、观测读数、校准证书与维修工单，按分区物化观测窗口。别名有效期、设备单位和质量规则影响观测是否进入产品。

演练通过本地设备配置接收读数，检查分区、质量与可追溯导出。

周期分析入口 `period.analyze` 直接使用实际观测时刻，不做等间隔插值：选择设备、指标、时间窗与频率范围（Hz）、分辨率后，按每个连续段各自的浮动均值拟合带浮动均值的广义 Lomb–Scargle 功率，返回各峰的周期、幅值、相位及相位时间原点，并同时给出采样谱窗以识别混叠。重复时刻按 `duplicate_time_policy`（mean/first）合并，质量标记按 `exclude_quality_codes` 排除，维修工单窗口默认剔除，各段以 `gap_seconds`（缺省取设备质量规则的 `max_gap_seconds`）为最大间隙切开，绝不跨长缺口连接。显著性在同一段内的连续时间块置换上复算整次搜索的最大峰，给出搜索校正后的假警概率，固定 `seed` 可复算。结果保留校准版本、原始观测与逐条排除原因；常量、覆盖太短、频率网格过密或病态拟合返回可解释诊断。

`python3 -m hangar_ingest --db :memory: period-demo` 在内存库中经正常接入路径运行本地演练，仅输出数值证据：真周期、相同采样时刻下的纯噪声，以及谱窗造成的伪峰。

主要代码位于 hangar_ingest，按 decoders、quality、runs、exports、analysis 划分职责；tests 保存领域行为、边界及存储回归用例。命令接收严格 JSON，详细操作名与字段可通过对应帮助入口及 service 中的注册表查询。示例中的机构、客户、案件和测量均为本地合成记录，不涉及真实个人资料。

使用 Python 3.11.2 与标准库，无第三方包。运行无需访问外部服务或提供账号。依赖清单 requirements.txt 为空，.python-version 固定解释器版本。

在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q hangar_ingest
python3 -m hangar_ingest --db :memory: demo --fixture fixtures/demo.json
python3 -m hangar_ingest --db :memory: period-demo
```

也可执行下面的统一检查，按顺序进行语法编译、领域回归和完整本地演练，任一环节失败即退出。运行写入均在临时目录或内存数据库，重复执行不需清理已有业务账。正常数据规模的检查可在 90 秒内完成。

```sh
sh scripts/check.sh
```
