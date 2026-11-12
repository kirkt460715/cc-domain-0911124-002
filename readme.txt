# 机队观测接入

管理机队设备、观测读数、校准证书与维修工单，按分区物化观测窗口。别名有效期、设备单位和质量规则影响观测是否进入产品。

演练通过本地设备配置接收读数，检查分区、质量与可追溯导出。

主要代码位于 hangar_ingest，按 decoders、quality、runs、exports 划分职责；tests 保存领域行为、边界及存储回归用例。命令接收严格 JSON，详细操作名与字段可通过对应帮助入口及 service 中的注册表查询。示例中的机构、客户、案件和测量均为本地合成记录，不涉及真实个人资料。

使用 Python 3.11.2 与标准库，无第三方包。运行无需访问外部服务或提供账号。依赖清单 requirements.txt 为空，.python-version 固定解释器版本。

在项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q hangar_ingest
python3 -m hangar_ingest --db :memory: demo --fixture fixtures/demo.json
```

也可执行下面的统一检查，按顺序进行语法编译、领域回归和完整本地演练，任一环节失败即退出。运行写入均在临时目录或内存数据库，重复执行不需清理已有业务账。正常数据规模的检查可在 90 秒内完成。

```sh
sh scripts/check.sh
```
