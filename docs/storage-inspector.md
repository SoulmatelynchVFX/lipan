# 璃盘：只读基础工具

这是从个人存储分析原型中整理出的独立基础模块。目前提供 Windows 应用清单、指定路径统计及按应用 ID 查看安装目录占用，不包含网页界面、全盘应用归属、规则知识库、历史数据库或 AI 建议。

Python 3.11+，无需第三方依赖。在仓库根目录运行：

```powershell
# 只读取卸载注册表，不扫描应用目录，不执行卸载命令
python -m storage_inspector --list-apps

# 仅在明确指定后遍历一个文件或目录
python -m storage_inspector --path "D:\Example"

# 将下面的 ID 替换成 --list-apps 返回的当前 app_id
python -m storage_inspector --app registry.REPLACE_WITH_APP_ID
```

结果为标准输出中的 JSON，不自动保存报告。不带参数只显示用法。程序不读取文件正文，不删除、移动文件或调用外部服务。

## 中断扫描

`--path` 或 `--app` 的目录遍历期间按一次 Ctrl+C，收到中断后会关闭活动目录迭代器，并输出已有观测数据：`stop_reason` 为 `interrupted`，`partial` 为真，退出码为 130。应用模式的停止原因位于 `scan.stop_reason`，顶层也会标记部分结果。零文件的中断结果不代表目录为空，中断不是访问错误，不增加 `error_count`。

若中断发生在应用清单读取或扫描初始化阶段，还没有可返回的扫描报告，只在标准错误输出提示并返回 130。清理或输出阶段再次中断、强制终止进程时，不保证报告完整；阻塞的系统调用也可能延迟响应。本版不保存续扫状态，重新运行会重新遍历。

## 如何理解结果

### 限制单次扫描

`--path` 和 `--app` 支持可选的文件数及时长限制，可分别使用或同时使用：

```powershell
python -m storage_inspector --path "D:\Example" --max-files 10000 --max-seconds 5
python -m storage_inspector --app registry.REPLACE_WITH_APP_ID --max-seconds 5
```

不指定时保留原有完整遍历行为。`--max-files` 必须为正整数，限制计入的去重普通文件数，不是目录数；`--max-seconds` 必须为有限正数，仅约束目录统计阶段，不包含读取应用清单的时间。纯清单模式 `--list-apps` 不接受这些参数。

触及上限时保留当前统计，`stop_reason` 为 `max_files` 或 `max_seconds`，`partial` 为真，退出码为 2；应用模式还会在 `scan` 中显示停止原因。这不是完整总量，也不是可清理容量。即使恰好计入最后一个文件，触及上限仍保守标为部分结果；未触发限制时 `stop_reason` 为 `null`，但读取错误或跳过项仍可能使 `partial` 为真。

时长为软限制：在遍历步骤之间检查，不能中断已阻塞的系统调用。目录按需逐项枚举，提前停止会关闭已打开的目录迭代器。不同遍历顺序可能得到不同的部分结果；本版不提供断点续扫。

### 统计边界

- `--app` 只扫描精确匹配 ID 的 `InstallLocation`，不是整盘搜索，也不根据卸载命令推导路径。只接受本地盘符下的绝对目录，拒绝盘符根、网络/设备路径、相对路径、环境变量和链接/离线根目录。没有安装位置、ID 无效或目录无法读取时返回 JSON 错误及退出码 2，不以零占用冒充成功。
- 应用结果的 `scope` 为 `install_location_only`，`application` 为所选清单项，`scan` 为该目录的统计或 `null`，`inventory` 保留清单读取诊断。清单或目录统计有缺口时顶层 `partial` 为真。缓存、用户数据及其他盘上的文件未自动计入；目录内也可能有共享文件，不能把多个应用结果直接相加。

- 应用清单来自 HKLM/HKCU 卸载项及 HKLM 的 WOW6432Node 项，隐藏系统组件不列出。不同版本或安装位置分别保留；仅名称、发行商、版本和规范化安装路径都相同的记录合并。缺少安装路径的记录按注册表键分别保留，不猜测属于哪个已知安装。
- `app_id` 由上述身份字段生成，不依赖枚举顺序。与旧版仅按名称和发行商生成的 ID 不兼容；版本、路径或未知安装的注册表键变化也会改变 ID，不应用它追踪跨升级历史。
- 应用清单新增 `partial`、`error_count`、`errors` 和 `missing_locations`。根键不存在会列入 `missing_locations`，不视为权限错误；读取失败会返回部分结果及退出码 2，保留最多 100 条错误明细。没有错误也不代表覆盖了全部安装方式或注册表视图。
- 未注册的软件、便携应用和无法读取的注册表项可能缺失。安装位置仅是线索，不代表应用全部数据；本版不做应用占用汇总。
- `logical_bytes` 是观测到的文件逻辑长度，不是实际磁盘分配量，更不是可清理容量。
- 单次统计按文件身份去重硬链接；跳过遇到的符号链接、Windows reparse point（含 junction）及离线/召回属性项，不主动读取云占位文件正文。
- `partial` 表示存在读取错误或跳过项。错误总数完整计数，明细最多保留 100 条。完整统计退出码为 0，部分结果及参数错误为 2。
- 扫描不是原子快照，文件同时变化可能影响结果；链接检查也不提供针对恶意并发替换的沙箱保证。不要在不可信进程持续改写的目录上运行。

JSON 会包含本机应用信息和绝对路径，不要直接提交或公开。需要保存时将报告放在已忽略的 `.runtime/` 目录，分享前自行脱敏。

## 验证

```powershell
python -m unittest discover -s tests -p "test_storage_inspector.py" -v
python -m unittest discover -s tests -p "test_inventory.py" -v
python -m unittest discover -s tests -p "test_app_scan.py" -v
python -m unittest discover -s tests -p "test_scan_limits.py" -v
python -m unittest discover -s tests -p "test_scan_interrupt.py" -v
```

测试只使用临时合成文件及模拟错误，不扫描真实应用数据，不执行清理。完整原型的后续功能尚未作为这个基础模块发布。
