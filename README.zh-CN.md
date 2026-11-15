# local-message-readers

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

local-message-readers 让你电脑上的 AI agent 直接读取你的 WeChat（微信）和 Microsoft Teams 聊天内容，不用再手动复制粘贴。它包含两个命令行工具 `wechat-reader` 和 `teams-reader`，把会话以 JSON 输出；另附一份现成的 agent skill（`message-reader`），告诉 agent 该怎么使用它们。

## 这是什么、为什么做

工作上的消息经常留在微信和 Teams 里，以前想交给 agent 看，只能自己先复制出来。这两个 reader 会打开桌面版软件本来就存放在你电脑上的数据，回答四个问题：有哪些会话、某个会话里有什么、哪些消息包含某个关键词、从某个时间点之后有什么新消息。

Teams 的缓存只保留最近一段时间的消息，所以 `teams-reader` 还会另外维护一份本地归档：每次查询先把 Teams 缓存合并进一个 SQLite 文件，Teams 清理之后消息依然留在归档里。更早的历史可以导入 Teams 官方导出文件来补充。

## 限制

- **只支持 Windows。** 数据位置、凭据加密（DPAPI）和每周备份（任务计划程序）都是 Windows 的功能。
- **只读不写。** 没有任何发送、修改或删除消息的功能。
- **Teams 只支持个人版账号。** 公司或学校的工作账号消息读不到。
- **只能读到这台电脑上已有的数据。** 桌面版软件关着或退出登录期间，那段时间的消息不会自动补回；请先在手机和桌面版之间同步聊天记录。reader 无法判断数据是否完整，重要时请拿最新一条消息和手机对照。
- **不支持：** 部分微信通话状态，以及图片内容（只返回标识符）。

## 安装（给非开发者）

你需要一台 Windows 11 电脑、至少登录过一次的微信和/或 Teams 桌面版，以及安装时可用的网络（依赖包从 GitHub 下载）。不需要自己装 Python，uv 会自动安装合适的版本。

下面所有命令都输入在 PowerShell 里（开始菜单输入 `PowerShell` 后回车）。

### 1. 安装 uv 并获取代码

1. 安装 [uv](https://docs.astral.sh/uv/)，它负责准备 Python 和依赖包：

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   装完请关掉 PowerShell 再重新打开，`uv` 才能被找到。
2. 获取代码。可以安装 [Git](https://git-scm.com/)，用本页绿色 **Code** 按钮显示的地址执行 `git clone <repository URL>`；也可以在 repository 页面选择 **Code > Download ZIP** 后解压。下面把你得到的文件夹称为 `<repo>`。
3. 安装依赖包：

   ```powershell
   cd <repo>
   uv sync
   ```

4. 确认可以运行：`uv run wechat-reader --help` 和 `uv run teams-reader --help` 都应该打印出使用说明。

可选：把 `<repo>` 里的 `settings.example.toml` 复制为 `settings.toml`，只填你需要的值。这个文件不进 git，每个字段都是可选的，文件里有各字段的说明。字段名拼错会让命令停下并指出是哪个字段。每个命令也支持 `--settings <文件>` 改读其他设置文件。

### 2. WeChat：获取密钥

微信的聊天数据库是加密的。请在微信桌面版开着、已登录的情况下，执行一次来获取密钥：

```powershell
cd <repo>
uv run wechat-cli init --db-dir "$env:USERPROFILE\Documents\xwechat_files\<你的账号文件夹>\db_storage"
```

`<你的账号文件夹>` 是 `xwechat_files` 下面含有 `db_storage` 的那个文件夹。密钥会保存在 repository 之外的 `%USERPROFILE%\.wechat-cli\all_keys.json`。微信升级后如果更换了数据库，需要重新执行这一步。底层工具的固定版本以微信 4.1.x 测试过。

然后试一试：

```powershell
uv run wechat-reader conversations
uv run wechat-reader recent --since "2026-01-01 09:00:00"
```

reader 不会记住已经给你看过什么；`recent` 的 `--since` 时间由调用方自己记录。

### 3. Teams：第一次同步

不需要密钥、不需要登录，Teams 也不用开着。第一次查询会把 Teams 缓存里的内容复制进本地归档（默认 `%LOCALAPPDATA%\local-message-readers\archive.db`）：

```powershell
cd <repo>
uv run teams-reader conversations
```

看输出 JSON 里的 `refresh.status` 和 `refresh.current_up_to`：它们会告诉你同步是否成功、数据新到什么时候。如果有 Teams 官方导出文件可以补充更早的历史，用 `uv run teams-reader import-export <导出文件>` 导入（导入前先备份，见第 5 步）。查询不会改变 Teams 里的未读状态；但你自己在 Teams 里打开某个会话，Teams 仍会把它标为已读。

如果配置文件夹不在标准位置，在 `settings.toml` 里设置 `profile_dir`，或加 `--profile-dir` 参数。

### 4. Teams：下载附件（第一次登录）

消息 JSON 会列出每个附件的 `name` 和 `url`。要下载文件，执行：

```powershell
uv run teams-reader download <message-id> --to <目标文件夹>
```

加 `--name <文件名>` 可以只下载其中一个。已存在的文件不会被覆盖，而是加上 ` (1)` 之类的后缀。

第一次下载会要求用**设备代码**登录：PowerShell 会显示一个 Microsoft 网址和一组短代码。用浏览器打开该网址、输入代码，并用你的个人 Microsoft 账户登录。细节：

- 登录使用 Microsoft Graph Command Line Tools 和 `Files.Read.All` 权限。这个权限可以读取你整个个人 OneDrive，附件才能取到。
- 凭据保存在 `%LOCALAPPDATA%\local-message-readers\graph-token-cache.bin`，用 DPAPI 加密并绑定你的 Windows 用户。
- **要从这台电脑上移除凭据**，删除那个文件。**要撤销授权本身**，打开 Microsoft 账户的隐私页面，进入“应用和服务”，移除 Microsoft Graph Command Line Tools。

### 5. 每周备份

Teams 会清理旧缓存，所以归档值得备份。每周的计划任务会更新归档，并把 Teams 归档和微信原始数据（含密钥）用 7-Zip 加密备份。你需要准备：

1. **7-Zip**（例如 `winget install 7zip.7zip`）。
2. **PowerShell 7**（`pwsh`，`winget install Microsoft.PowerShell`）。
3. **备份密码**，保存在 Windows 凭据管理器里。在命令提示符中执行下面的命令，按提示输入密码：

   ```cmd
   cmdkey /generic:local-message-readers/archive-backup /user:backup /pass
   ```

   只能使用英文字母、数字和键盘上的符号；不能包含双引号 `"`、不能包含非英文字符，也不能以反斜杠结尾。请把密码记在你的密码管理器里：换新电脑恢复时需要它。
4. **一个已存在的目标文件夹**（例如 OneDrive 里的文件夹）。在 `settings.toml` 里设置 `backup_dest`，或加 `--dest` 参数。
5. **计划任务**，在项目主文件夹里注册一次：

   ```powershell
   pwsh -File scripts/register-backup-task.ps1 -Destination "<已存在的文件夹>"
   ```

   它每周日中午 12:00 在你登录时运行；如果当时电脑关机，下次登录后补跑。保留最新的 8 份备份。

想立刻备份一次：`uv run python -m readers.backup --dest "<已存在的文件夹>"`。请检查结果里的 `wechat.ok`；为 false 时，那份备份只包含 Teams。恢复步骤、退出码和已知限制见 [`docs/teams-archive-backup.md`](docs/teams-archive-backup.md)。每次 Teams 查询也会报告 `backup.stale`；超过 8 天没有成功备份时为 true。

## 让 agent 使用

`skills/message-reader/` 里的 skill 教 agent 查询微信和 Teams、确认数据新鲜度、处理重名会话，并在数据可能不完整时提醒你。请让你的 agent 能看到这个文件夹：

- **Claude Code：** 在你要使用它的项目或用户目录的 skills 文件夹里创建链接：

  ```powershell
  New-Item -ItemType Junction -Path "<项目或 %USERPROFILE%>\.claude\skills\message-reader" -Target "<repo>\skills\message-reader"
  ```

- **其他 agent：** 让 agent 读取 `skills/message-reader/SKILL.md`，或把内容粘贴进 agent 的指令里。

在任何其他文件夹里，agent 都用 `uv run --project <repo> wechat-reader ...` 和 `uv run --project <repo> teams-reader ...` 运行 reader，所以要告诉它 `<repo>` 在哪里。查询输出会写进 `<repo>/fixtures/local/`，git 会忽略这个文件夹。每个命令和参数的完整说明在 `AGENTS.md`。

## 隐私

- 数据全部留在你的电脑上：reader、归档、密钥和备份都是本地文件。如果备份目标选在云同步文件夹（例如 OneDrive）里，加密后的备份文件会由该服务同步到云端。
- reader 唯一的对外连接是 Microsoft Graph，而且只在你下载 Teams 附件时才连。（`uv sync` 在安装时会下载一次依赖包，那不是消息数据。）
- 本项目不会把消息内容发送到任何地方。agent 之后如何处理这些内容，取决于你接入的是哪个 agent。
- 不要提交聊天内容、密钥或你的 `settings.toml`；repository 默认会忽略它们。

## 致谢

- [wechat-cli](https://github.com/huohuoer/wechat-cli)（Apache-2.0）负责读取微信数据库并获取密钥。本项目从一个固定版本的 fork 安装它，上游消失时安装依然可用。
- CCL Forensics 的 [ccl_chrome_indexeddb](https://github.com/cclgroupltd/ccl_chrome_indexeddb) 和 [ccl_simplesnappy](https://github.com/cclgroupltd/ccl_simplesnappy)（MIT）负责读取 Teams 存放缓存的浏览器式数据库。
- [uv](https://docs.astral.sh/uv/) 和 [MSAL for Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) 负责环境管理和 Microsoft 登录。

## 许可证

[MIT](LICENSE)。
