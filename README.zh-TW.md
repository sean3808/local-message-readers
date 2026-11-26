# local-message-readers

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

local-message-readers 讓你電腦上的 AI agent 直接讀取你的 WeChat 與 Microsoft Teams 聊天內容，不必再手動複製貼上。它包含兩個命令列工具 `wechat-reader` 與 `teams-reader`，把對話以 JSON 輸出；另附一份現成的 agent skill（`message-reader`），教 agent 怎麼使用它們。

## 這是什麼、為什麼做

工作上的訊息常常留在 WeChat 和 Teams 裡，以前要交給 agent 看，只能自己先複製出來。這兩個 reader 會打開桌面版軟體本來就存在你電腦上的資料，回答四個問題：有哪些對話、某個對話裡有什麼、哪些訊息含有某個關鍵字、從某個時間點之後有什麼新訊息。

Teams 的快取只保留最近一段時間的訊息，所以 `teams-reader` 另外維護一份本機封存：每次查詢先把 Teams 快取併進一個 SQLite 檔，Teams 清掉之後訊息仍留在封存裡。更早的歷史可以匯入 Teams 官方匯出檔補進來。

## 限制

- **只支援 Windows。** 資料位置、憑證加密（DPAPI）與每週備份（工作排程器）都是 Windows 的功能。
- **只讀不寫。** 沒有任何發送、修改或刪除訊息的功能。
- **Teams 只支援個人版帳號。** 公司或學校的工作帳號訊息讀不到。
- **只讀得到這台電腦上已有的資料。** 桌面版軟體關著或登出的期間，那段時間的訊息不會自動補回；請先在手機與桌面版之間同步聊天記錄。reader 無法判斷資料是否完整，重要時請拿最新一則訊息跟手機對照。
- **不支援：** 部分 WeChat 通話狀態，以及圖片內容（只回傳識別碼）。

## 安裝（給非開發者）

你需要一台 Windows 11 電腦、至少登入過一次的 WeChat 和／或 Teams 桌面版，以及安裝時可用的網路（相依套件從 GitHub 下載）。不需要自己裝 Python，uv 會自動裝對的版本。

下面所有指令都輸入在 PowerShell（開始功能表輸入 `PowerShell` 後按 Enter）。

### 1. 安裝 uv 並取得程式碼

1. 安裝 [uv](https://docs.astral.sh/uv/)，它負責準備 Python 與相依套件：

   ```powershell
   winget install --id=astral-sh.uv -e
   ```

   裝完請關掉 PowerShell 再重開，`uv` 才找得到。
2. 取得程式碼。可以安裝 [Git](https://git-scm.com/)，用本頁綠色 **Code** 按鈕顯示的位址執行 `git clone <repository URL>`；也可以在 repository 頁面選 **Code > Download ZIP** 後解壓縮。下面把你得到的資料夾稱為 `<repo>`。
3. 安裝相依套件：

   ```powershell
   cd <repo>
   uv sync
   ```

4. 確認可以執行：`uv run wechat-reader --help` 與 `uv run teams-reader --help` 都應該印出使用說明。
5. 把 reader 安裝成任何資料夾都能執行的指令：`uv tool install <repo>`。之後 `wechat-reader --help` 與 `teams-reader --help` 在哪裡都能執行。更新程式碼後，執行 `uv tool install --reinstall <repo>`，指令才會用到新版。

選用：把 `<repo>` 裡的 `settings.example.toml` 複製成 `%LOCALAPPDATA%\local-message-readers\settings.toml`，只填你需要的值（如果你原本在 `<repo>` 放了 `settings.toml`，請把它搬過去；`<repo>` 裡的複本不會再被讀取）。每個欄位都是選填，檔內有各欄位的說明。欄位名稱拼錯會讓指令停下並指出是哪個欄位。每個指令也接受 `--settings <檔案>` 改讀別的設定檔。

### 2. WeChat：取得金鑰

WeChat 的聊天資料庫是加密的。請在 WeChat 桌面版開著、已登入的情況下，執行一次取得金鑰：

```powershell
cd <repo>
uv run wechat-cli init --db-dir "$env:USERPROFILE\Documents\xwechat_files\<你的帳號資料夾>\db_storage"
```

`<你的帳號資料夾>` 是 `xwechat_files` 底下含有 `db_storage` 的那個資料夾。金鑰會存在 repository 之外的 `%USERPROFILE%\.wechat-cli\all_keys.json`。WeChat 升級後若更換了資料庫，要重跑這一步。底層工具的固定版本以 WeChat 4.1.x 測試過。

然後試試看：

```powershell
uv run wechat-reader conversations
uv run wechat-reader recent --since "2026-01-01 09:00:00"
```

reader 不會記得已經給你看過什麼；`recent` 的 `--since` 時間由呼叫的人自己記。最穩的做法是把上次讀到最後一則訊息的 `time` 原樣傳回去：它帶著時差，電腦換了時區也還是指向同一個時刻。沒寫時差的時間會當成這台電腦的當地時間。公眾號（訂閱號、服務號）不會出現在 `conversations`、`search` 與 `recent` 的結果裡。

### 3. Teams：第一次同步

不需要金鑰、不需要登入，Teams 也不必開著。第一次查詢會把 Teams 快取裡的內容複製進本機封存（預設 `%LOCALAPPDATA%\local-message-readers\archive.db`）：

```powershell
cd <repo>
uv run teams-reader conversations
```

看輸出 JSON 裡的 `refresh.status` 與 `refresh.current_up_to`：它們會告訴你同步是否成功、資料新到什麼時候。若距離同一個 Teams 設定檔上次乾淨的更新（成功且沒有 `failures`）不到 `refresh_min_interval_seconds`（預設 `120` 秒，設在 `%LOCALAPPDATA%\local-message-readers\settings.toml`，設為 `0` 則每次都更新），會略過更新（`refresh.status` 為 `"skipped"` 並附帶 `refresh.reason`）。加 `--refresh` 可強制更新，加 `--no-refresh` 會一律略過（同時傳兩者會報錯）；做多步查詢時，可以在一次 `search` 中傳入多個關鍵字，或先用 `--refresh` 更新一次，後續查詢再加 `--no-refresh`。`messages` 與 `search` 都會保留最新的 `--limit` 筆結果（依由舊到新排列）並回報 `truncated`。如果有 Teams 官方匯出檔可以補更早的歷史，用 `uv run teams-reader import-export <匯出檔>` 匯入（匯入前先備份，見步驟 5）。查詢不會改變 Teams 裡的未讀狀態；但你自己在 Teams 裡打開某個對話，Teams 還是會把它標成已讀。

如果設定檔資料夾不在標準位置，在 `%LOCALAPPDATA%\local-message-readers\settings.toml` 設 `profile_dir`，或加 `--profile-dir` 參數。

依顯示名稱查詢對話時，若有多個一對一對話同名且 `last_time` 各自不同，`teams-reader` 會自動選取最新的一個，並把其餘候選列在 `alternatives`（`[{id, last_time}]`）；若其中包含群組或有兩個 `last_time` 相同，仍會回傳錯誤並列出各個 `id` 供指定。

### 4. Teams：下載附件（第一次登入）

訊息 JSON 會列出每個附件的 `name` 與 `url`。要下載檔案，執行：

```powershell
uv run teams-reader download <message-id> --to <目的資料夾>
```

加 `--name <檔名>` 可以只下載其中一個。已存在的檔案不會被覆寫，而是加上 ` (1)` 之類的後綴。

第一次下載會要求用**裝置代碼**登入：PowerShell 會顯示一個 Microsoft 網址與一組短代碼。用瀏覽器開啟該網址、輸入代碼，並用你的個人 Microsoft 帳號登入。細節：

- 登入使用 Microsoft Graph Command Line Tools 與 `Files.Read.All` 權限。這個權限可以讀取你整個個人 OneDrive，附件才抓得到。
- 憑證存在 `%LOCALAPPDATA%\local-message-readers\graph-token-cache.bin`，用 DPAPI 加密並綁定你的 Windows 使用者。
- **要從這台電腦移除憑證**，刪掉那個檔案。**要撤銷授權本身**，開啟 Microsoft 帳戶的隱私權頁面，進入「應用程式與服務」，移除 Microsoft Graph Command Line Tools。

### 5. 每週備份

Teams 會清掉舊的快取，所以封存值得備份。每週的排程會更新封存，並把 Teams 封存與 WeChat 原始資料（含金鑰）用 7-Zip 加密備份。你要準備：

1. **7-Zip**（例如 `winget install 7zip.7zip`）。
2. **PowerShell 7**（`pwsh`，`winget install Microsoft.PowerShell`）。
3. **備份密碼**，存放在 Windows 認證管理員。在命令提示字元執行下面指令，依提示輸入密碼：

   ```cmd
   cmdkey /generic:local-message-readers/archive-backup /user:backup /pass
   ```

   只能用英文字母、數字與鍵盤上的符號；不可含雙引號 `"`、不可含非英文字元，也不可以反斜線結尾。請把密碼記在你的密碼管理員：換新電腦還原時需要它。
4. **一個已存在的目的資料夾**（例如 OneDrive 裡的資料夾）。在 `%LOCALAPPDATA%\local-message-readers\settings.toml` 設 `backup_dest`，或加 `--dest` 參數。
5. **排程**，從專案主資料夾註冊一次：

   ```powershell
   pwsh -File scripts/register-backup-task.ps1 -Destination "<已存在的資料夾>"
   ```

   它每週日中午 12:00 在你登入時執行；電腦當時關機的話，下次登入後補跑。保留最新 8 份備份。

要立刻備份一次：`uv run python -m readers.backup --dest "<已存在的資料夾>"`。請檢查結果裡的 `wechat.ok`；為 false 時，那份備份只含 Teams。還原步驟、結束代碼與已知限制見 [`docs/teams-archive-backup.md`](docs/teams-archive-backup.md)。每次 Teams 查詢也會回報 `backup.stale`；超過 8 天沒有成功備份時為 true。

## 讓 agent 使用

`skills/message-reader/` 裡的 skill 教 agent 查詢 WeChat 與 Teams、確認資料新鮮度、處理重名對話，並在資料可能不完整時提醒你。請讓你的 agent 看得到這個資料夾：

- **Claude Code：** 在你要使用它的專案或使用者目錄的 skills 資料夾裡建立連結：

  ```powershell
  New-Item -ItemType Junction -Path "<專案或 %USERPROFILE%>\.claude\skills\message-reader" -Target "<repo>\skills\message-reader"
  ```

- **其他 agent：** 讓 agent 讀 `skills/message-reader/SKILL.md`，或把內容貼進 agent 的指示裡。

從任何其他資料夾，agent 都用已安裝的指令 `wechat-reader ...` 與 `teams-reader ...` 執行 reader（步驟 1 的第 5 項）。仍要告訴它 `<repo>` 在哪裡。查詢輸出會寫進 `<repo>/fixtures/local/`，git 會忽略這個資料夾。每個指令與參數的完整說明在 `AGENTS.md`。

## 隱私

- 資料全部留在你的電腦：reader、封存、金鑰與備份都是本機檔案。如果備份目的地選在雲端同步資料夾（例如 OneDrive）裡，加密後的備份檔會由該服務同步上雲。
- reader 唯一的對外連線是 Microsoft Graph，而且只在你下載 Teams 附件時才連。（`uv sync` 在安裝時會下載一次相依套件，那不是訊息資料。）
- 本專案不會把訊息內容送到任何地方。agent 之後怎麼處理這些內容，取決於你接上的是哪個 agent。
- 不要提交聊天內容、金鑰或你的 `settings.toml`；repository 預設會忽略它們。

## 致謝

- [wechat-cli](https://github.com/huohuoer/wechat-cli)（Apache-2.0）負責讀取 WeChat 資料庫並取得金鑰。本專案從一個固定版本的 fork 安裝它，上游消失時安裝仍然可用。
- CCL Forensics 的 [ccl_chrome_indexeddb](https://github.com/cclgroupltd/ccl_chrome_indexeddb) 與 [ccl_simplesnappy](https://github.com/cclgroupltd/ccl_simplesnappy)（MIT）負責讀取 Teams 存放快取的瀏覽器式資料庫。
- [uv](https://docs.astral.sh/uv/) 與 [MSAL for Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) 負責環境管理與 Microsoft 登入。

## 授權

[MIT](LICENSE)。
