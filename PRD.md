# PRD — AppImage for Omarchy(Omarchy AppImage 管理插件)

> **版本:** v0.4(加入 Rust 評估結論:唔採用)
> **日期:** 2026-08-27
> **參考:** [GearLever](https://github.com/mijorus/gearlever)(GPL-3.0,mijorus)・[Omarchy Plugin Dev Guide](https://omarchyplugins.com/develop.html)

---

## 1. 背景與目標

[GearLever](https://github.com/mijorus/gearlever) 係一個好受歡迎(~2.1k stars)嘅 Linux AppImage 管理器:一鍵將 AppImage 整合入 app menu、管理更新、拖放安裝。佢係一個獨立嘅 GTK app(閒置食 200MB+ RAM),同 desktop 環境冇深度整合。

Omarchy(Arch + Hyprland)嘅 shell 係 Quickshell(QML),我哋想要一個 **原生 Omarchy plugin 版本**:直接喺 status bar 同 shell panel 入面管理 AppImage,而且**輕過 GTK app 一個量級**。

### 目標

1. 用 Omarchy plugin(QML)重現 GearLever 嘅核心功能:integrate、list、update、remove
2. 提供 Omarchy 原生體驗:bar widget、panel、背景 service、桌面通知
3. Backend 邏輯拆自 GearLever CLI(GPL-3.0),但**剝走所有第三方依賴**,只靠 Python stdlib
4. 發佈上 [omarchy plugin marketplace](https://omarchyplugins.com/)

### 非目標(Out of Scope)

- 唔支持 AppImage 以外格式(deb / rpm / Arch package)— 同 GearLever 一致
- 唔做 GUI 版 GearLever 嘅所有設定項(例如主題、進階過濾)
- 唔會修改 `/usr/share/omarchy/`(Omarchy 規定)

---

## 2. 設計宗旨:輕量化承諾(硬性預算)

呢個唔係口號,係驗收指標。任何功能如果令下面任何一項超標,要么砍功能、要么降級做 P2:

| 資源 | 預算 | 點做到 |
|---|---|---|
| 常駐 RAM | **0 MB**(shell process 之外) | Backend 用完即棄:每次 action 先 spawn Python 子進程(~10–20MB,<1 秒完結);service 只係 QML 一個 Timer(幾 KB) |
| CPU | 無輪詢、無 busy-wait | Update check 每 6 小時一次(可配置);列表用快取,唔係次次播 panel 都重掃 |
| Storage | **Plugin 本體 < 1 MB** | QML 幾十 KB + Python 二三百 KB;**無 `vendor/` 目錄**(零第三方庫);快取 JSON < 100 KB |
| Network | 只有 update check 同用戶要求嘅下載 | 無 telemetry、無 analytics |
| 依賴 | **零新增 pacman 包(目標)** | 見 §7.3:**淨 `python3`**(原定嘅 bsdtar 實測讀唔到 squashfs,已改自寫純 Python reader)|

**對標:** GearLever flatpak 開住 ~200MB+ 常駐;本 plugin 閒置零額外佔用。

---

## 3. 目標用戶

- Omarchy 用戶,慣用 AppImage 裝唔喺 repo 嘅 app(例如 VS Code 分叉、小眾工具)
- 想要「download 完 AppImage → 一個掣搞掂」體驗嘅人
- 鍾意 bar 有一個 icon 隨時顯示有冇 update、同時在意資源佔用嘅 power user

---

## 4. 產品定位與命名(已決策)

| 項目 | 決定 |
|---|---|
| 顯示名稱 | **AppImage for Omarchy** |
| Plugin ID | `io.github.glasschan.appimage`(namespaced,唔可以用 `omarchy.*`) |
| License | **GPL-3.0** — backend 拆自 GearLever 衍生代碼,整個 plugin 必須同 license |

---

## 5. 功能範圍

由 GearLever 功能映射到 Omarchy plugin 形態。優先級:**P0 = MVP 必須,P1 = v1.0,P2 = 後續**。

| # | 功能 | GearLever 對應 | 優先級 | Omarchy 形態 |
|---|---|---|---|---|
| F1 | 已整合 AppImage 列表(name、version、icon、running 狀態) | `--list-installed` | **P0** | Panel 主列表 |
| F2 | 一鍵 integrate 新 AppImage(抽取 .desktop + icon、搬去管理資料夾) | `--integrate` | **P0** | Panel 掣 → file picker |
| F3 | 移除(AppImage + .desktop + icons 入 trash) | `--remove` | **P0** | Panel 列表 item action |
| F4 | 啟動 AppImage | UI「Open」 | **P0** | Panel item click |
| F5 | Bar widget:icon + 已裝數量 / 有 update 嘅 badge | — | **P0** | bar-widget,click 開 panel |
| F6 | Update 檢查(embedded source + custom source) | `--list-updates` | **P1** | Panel + service |
| F7 | 一鍵 update(可選保留舊版) | `--update` | **P1** | Panel item action |
| F8 | 背景 update 檢查 + 桌面通知 | `--fetch-updates` | **P1** | service kind(Timer)+ Omarchy 通知 |
| F9 | 設定/修改 custom update source | `--set-update-source` | **P1** | Panel 設定頁 |
| F10 | Downloads 資料夾偵測新 AppImage → bar 提示「有新 AppImage 未整合」 | — | **P2**(要證明唔違反 §2 CPU 預算先做) | service + badge |
| F11 | 拖放 AppImage 入 panel 直接 integrate | GTK drag & drop | **P2** | 視 Quickshell 支援而定 |
| F12 | 與上游 GearLever 同步(cherry-pick 重要 fixes) | — | **P2** | 維護工作,非用戶功能 |
| F13 | FTP update source | `ftputil` | **P2** | urllib FTP 補,好少人用,唔阻 MVP |

### 用戶故事(MVP)

1. 「我 download 咗個 `Foo-x86_64.AppImage`,撳 bar 個 icon,撳 *Integrate*,揀個 file,搞掂 — app menu 見到 Foo,有 icon。」
2. 「我喺 panel 睇到邊啲 AppImage 裝咗、邊個 running,唔要嘅撳一下就 trash 咗。」
3. 「有 update 嗰陣,bar icon 有 badge,開 panel 撳 *Update* 就升級。」

---

## 6. UX 設計(概要)

```
┌─ Bar(右側)─────────────────────────────────────────┐
│  ... [📦 AppImage (●3)] ← bar-widget:icon + 數字/badge   │
└──────────────────────────┬──────────────────────────┘
                           │ click
┌──────────────────────────▼──────────────────────────┐
│ Panel: AppImages                    [+ Integrate] ⚙ │
│ ┌─────────────────────────────────────────────────┐ │
│ │ 🖼 Foo 1.2.3        ▸ Launch  ↻ Update  🗑 Remove│ │
│ │ 🖼 Bar  0.9.0  ●running                            │ │
│ │ 🖼 Baz  2.0.1   ⬆ 1.1 available                  │ │
│ └─────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

- **bar-widget**(`BarWidget.qml`):icon;badge = 已整合數量,有 update 時轉為 update 數(橙色)
- **panel**(`Panel.qml`):主 UI,列表 + actions;要正確轉發 lifecycle(`opened` / `open()` / `close()` / `toggle()` / `popoutSwitchClosing` / `closeForPopoutSwitch()`),Escape 用 `PanelKeyCatcher`
- **service**(`Service.qml`,P1):Timer 定時 check update,有更新就發 Omarchy 桌面通知
- 沿用 Omarchy 主題色(qs.Theme),唔硬編顏色

---

## 7. 技術架構

### 7.1 技術選型:點解係 QML + Python stdlib

**UI = QML(唯一選擇)。** Omarchy plugin runtime 就係 Quickshell 嘅 QML 引擎,bar-widget / panel / service 三種 kind 都係 QML 檔。呢個係平台規定,唔係偏好。

**Backend = Python 3 純 stdlib 子進程。** QML 用 Quickshell `Process` call:

```sh
python3 <plugin_dir>/backend/main.py --list-installed --json
python3 <plugin_dir>/backend/main.py --integrate /path/to/App.AppImage --yes
python3 <plugin_dir>/backend/main.py --update <desktop-id> --yes
python3 <plugin_dir>/backend/main.py --remove <desktop-id>
```

點解揀 Python:

1. **可以直接拆 GearLever 邏輯嚟用** — 佢係 Python,updaters(GitHub/Gitlab/Codeberg/Forgejo)、版本比對、AppImage integration 規則呢啲經過實戰嘅邏輯直接復用;揀 Rust/Go 就要全部由零重寫,仲要維護編譯鏈同發佈 binary
2. **stdlib 包晒所需** — HTTP+TLS(`urllib`)、.desktop/INI 讀寫(`configparser`)、AppImage ELF offset 計算(`struct`)、atomic 寫檔(`os.replace`)全部 built-in → 零第三方依賴
3. **進程隔離** — Omarchy plugin 無 sandbox,同你個 bar 同一個 process;backend 行獨立子進程,parse 炸咗/下載炒咗都唔會攬炒成個 shell,重活亦唔會卡住 bar
4. **用完即棄,零常駐** — 每次 action 先 spawn,完結即走,符合 §2 預算

點解**唔**用其他方案:

| 方案 | 唔揀嘅原因 |
|---|---|
| 純 QML / JS 一腳踢 | Binary 處理(ELF header 解析、squashfs 抽取、下載 100MB AppImage 寫碟)喺 QML 好辛苦;而且要喺 shell process 內做,出事攬炒 bar |
| Rust / Go 編譯 binary | 詳細評估見 §7.1.1 — 總結:binary 大細打穿 §2 嘅 < 1MB 預算、成個 GearLever 邏輯要由零重寫、維護成本高;唔採用 |
| Bash | JSON API 解析、ELF 解析、atomic file ops 喺 bash 又長又脆 |
| 保留 GearLever flatpak CLI | 要求用戶裝 flatpak 版、每次 call ~1s flatpak 開銷、依賴佢人哋嘅發佈週期 |

#### 7.1.1 Rust 評估(2026-08-27,結論:唔採用)

Rust 確實擅長常駐進程、hot path、系統級工具。但對呢個項目,逐項計過:

| 考量 | Rust 實況 | Python stdlib 實況 | 判斷 |
|---|---|---|---|
| Binary 大細 | Updaters 全部要 HTTPS → 必須帶 TLS stack(reqwest + rustls 等),stripped 都 **5–15MB**,直接打穿 §2 嘅 < 1MB 硬預算 | 源碼二三百 KB;TLS 由 stdlib 連住系統 OpenSSL,零額外體積 | Python 勝 |
| 邏輯復用 | GearLever 邏輯(updaters、版本比對、integration 規則)係 Python,**要全部由零重寫** — 呢啲正正係實戰累積嘅 edge case,重寫 = 重新踩一次晒啲坑 | 直接拆嚟用 | Python 勝(呢個係項目核心價值) |
| 啟動速度 | ~1–3ms | ~50–150ms(import 後) | 平手 — backend 一日先 call 幾次,UI 行 cache-first,差嗰 100ms 感知唔到 |
| RAM | 子進程 ~2MB、用完即棄 | 子進程 ~10–20MB、用完即棄 | 平手 — 兩樣都係 0 常駐,符合 §2 |
| 發佈/更新 | 要維護編譯鏈、喺 plugin repo 放預編 binary(用戶無得審源碼 runtime 行咩)、支援 x86_64/aarch64 兩套 | git pull 文字檔就係更新 | Python 勝 |

**Escape hatch:** backend 收埋喺一個穩定嘅 CLI `--json` 介面後面,QML 淨係同呢個介面傾嘢。將來真有任何理由要換 Rust(例如 Python 唔見咗喺某啲迷你安裝),可以淨換 backend、QML 一行都唔使改 — 而家唔使為咗呢個可能性買單。

### 7.2 Plugin 結構(跟 dev guide)

```
~/.config/omarchy/plugins/io.github.glasschan.appimage/
├── manifest.json
├── BarWidget.qml
├── Panel.qml
├── Service.qml          # P1:背景 update 檢查(Timer)
├── lib/
│   ├── Backend.js       # Process wrapper + JSON 解析 + 快取
│   └── Model.js         # 列表 state
├── backend/             # 拆自 GearLever 嘅 Python stdlib-only CLI
│   ├── main.py          # CLI-only 入口
│   └── ...(手術後嘅模組;無 vendor/,只有自己嘅 .py)
├── README.md
├── LICENSE              # GPL-3.0
└── preview.png
```

`manifest.json` 核心:

```json
{
  "schemaVersion": 1,
  "id": "io.github.glasschan.appimage",
  "name": "AppImage for Omarchy",
  "version": "0.1.0",
  "author": "glasschan",
  "license": "GPL-3.0",
  "description": "Manage AppImages from the Omarchy bar: integrate, update, remove.",
  "kinds": ["bar-widget", "panel", "service"],
  "entryPoints": {
    "barWidget": "BarWidget.qml",
    "panel": "Panel.qml",
    "service": "Service.qml"
  },
  "barWidget": {
    "displayName": "AppImage",
    "category": "Apps",
    "allowMultiple": false,
    "defaultSection": "right"
  }
}
```

用戶設定(管理資料夾、update 頻率)存 `~/.config/io.github.glasschan.appimage/settings.json`,唔用 GSettings。

### 7.3 依賴(最終清單:淨一樣)

| 依賴 | 來源 | 用途 |
|---|---|---|
| `python3` | Arch/Omarchy 實機已驗證(3.14.7 在);真係冇就 `omarchy pkg add python` | Backend runtime(連 SquashFS 讀取都係純 Python) |

> **設計修正(2026-08-27 實測推翻原方案):** 原本諗住用 `bsdtar` 抽 squashfs,但 **libarchive 根本冇 squashfs reader**(bsdtar 3.8.9 實測 "Unrecognized archive format";呢部機亦冇 unsquashfs)。解法:自寫純 Python SquashFS 4.0 reader(superblock/fragments/DIR/LDIR/REG/LREG/SYMLINK inode、跨 block stream;zlib/lzma/zstd),對 neovim AppImage 官方 `--appimage-extract` ground truth 做 byte-for-byte 比對,2057 個檔全中。`unsquashfs`/`bsdtar` 只留做 lzo/lz4 罕見壓縮嘅 fallback 鏈。

**全部剝走:** `python-gobject`、`dbus-python`、`requests`、`pyxdg`、`desktop-entry-lib`、`ftputil`。**最終運行時依賴:淨 `python3`。**

### 7.4 拆骨手術表(由 GearLever master,2026-08-27 調查)

GearLever CLI 同 UI 共用核心模組,拆出嚟要將 infra 層全部換成 stdlib:

| 模組 | 現狀 | 手術 |
|---|---|---|
| `src/Cli.py`、`models/UpdateManager*.py`、`Models.py`、`BackgroudUpdatesFetcher.py` | 乾淨(只有 stdlib import) | 直接抄 |
| `lib/constants.py`、`ini_config.py`、`json_config.py` | 用 `GLib`(用戶目錄等) | 換 `os` / `pathlib` / `configparser` |
| `lib/async_utils.py` | `GLib, GObject` | CLI 同步執行,整個唔要 |
| `lib/utils.py` | 最重:`Gtk, Gio, Adw, Gdk, GdkPixbuf` + `dbus`(portal) | Gtk/Adw/Gdk 用途 stub;icon 原樣照搬唔做轉換(desktop entry 指絕對路徑);trash 按 FreeDesktop spec 自己寫(`~/.local/share/Trash/`);portal 唔要 |
| `models/Settings.py` | `Gio`(GSettings) | 換自己嘅 JSON settings(§7.2) |
| `providers/AppImageProvider.py` | `GLib, Gtk, Gdk, Gio`;squashfs 抽取 | Gtk/Gdk stub;抽取改用**自寫純 Python SquashFS reader**(見 §7.3);ELF offset 用 `struct` 重寫(參考佢 `build-aux/get_appimage_offset.sh`);**絕不執行 AppImage 本身** |
| `models/*Updater.py`(GitHub/Gitlab/…) | `Adw, Gio`(toast/通知)+ `requests` | Adw 換 JSON 輸出;`requests` 換 `urllib.request` |

**維護策略:** backend 放同一 repo 嘅 `backend/`;上游有重要 fix 先 cherry-pick,唔追住 upstream 跑。

### 7.5 開發與驗證工作流

```sh
omarchy plugin clone omarchy.clock --edit   # 攞個 built-in 做骨架起手
omarchy plugin validate "$PLUGIN_DIR"       # manifest 驗證(必須過)
qmllint -I "$OMARCHY_PATH/shell" "$PLUGIN_DIR"/*.qml
omarchy-shell shell rescanPlugins           # 強制重載(hot-reload 唔生效時)
omarchy-shell shell summon "$PLUGIN_ID" '{}'  # 測試 panel lifecycle
```

Backend smoke test 用 GearLever 官方 test files([gearlever-test-files](https://github.com/mijorus/gearlever-test-files),佢哋 `tests/test_cli.py` 都係用呢套)。

發佈前要測:click、Escape、open/close、disable/enable、shell restart、remove plugin。

---

## 8. 非功能需求

| 類別 | 要求 |
|---|---|
| 效能 | Panel 開嘅時候列表由快取即時 render,refresh 先 async call backend;bar widget 唔可以 block shell;任何 backend call 設 timeout |
| 安全 | Plugin 無 sandbox(共用 shell process、用戶權限)— 只 call 明確嘅 CLI、路徑要 escape、唔好用 `sh -c` 拼接用戶輸入;從 AppImage 抽檔案用 `bsdtar`,**絕不執行 AppImage 本身** |
| 輸出純淨度 | `--json` 模式下 backend stdout 只可以係 JSON(logging 去 stderr),方便 QML 解析 |
| 相容 | Quickshell / Quattro API;唔開第二個 Quickshell process(dev guide 明文禁止) |
| 授權 | GPL-3.0(backend 係 GearLever 衍生代碼) |
| i18n | MVP 英文;介面字串集中喺一處方便之後加 zh-HK |
| 穩定 | backend 錯誤要喺 panel 顯示,唔可以靜靜哋失敗 |

---

## 9. 風險

| 風險 | 影響 | 緩解 |
|---|---|---|
| 手術比預期深(v0.3 要連 `GLib`、`requests` 都換走,`utils.py` 污染最勁) | M1a 延期 | 第一步先通 `--list-installed --json` 一條路徑驗證;拆唔郁嘅個別 function 直接 stub 返回預設值 |
| ~~`python3` 唔保證喺 Arch base~~ **已解** | — | 真 Omarchy 實機已驗證(3.14.7 在);QML 端 spawn-failure 時有 `omarchy pkg add python` 提示(JS harness 有專測) |
| 上游 GearLever 演進,extracted copy 會舊 | 功能落後 | 接受;有 bug 先 cherry-pick(F12) |
| `urllib` 換走 `requests` 可能撞到邊啲 edge case(redirect、chunked、TLS) | update 檢查失敗 | 用官方 test files 覆蓋;`requests` 對呢啲場景其實都係包住 urllib 行為,風險有限 |
| Quickshell `Process` / file dialog API 未必有齊(file picker 點開係未知數) | F2 做法 | Spike 驗證;fallback 用 portal file dialog、text input 貼路徑、或 P2 嘅 Downloads 偵測 |
| 拖放(Quickshell drop target)可能唔支持 | F11 | 定為 P2,先確認可行性 |
| GPL-3.0 傳染性 | 授權 | 成個 plugin 已定 GPL-3.0,marketplace 無問題 |

---

## 10. 決策記錄

**2026-08-27(第一輪):**
1. **命名 →「AppImage for Omarchy」**,ID `io.github.glasschan.appimage`
2. **Backend → 拆 GearLever CLI 出嚟打包入 plugin**(唔係包 flatpak、唔係由零重寫)
3. **Update 檢查** = 檢查已裝 AppImage 有冇新版本(embedded source 或自定 URL),有就 badge + 通知。頻率 default:login 後 5 分鐘第一次,之後每 6 小時;panel 有手動 refresh,設定可改
4. **Namespace → `glasschan`**

**2026-08-27(第二輪,輕量化):**
5. **技術棧 → QML(UI,平台唯一選擇)+ Python 3 純 stdlib(backend)**,理由見 §7.1
6. **輕量化係硬性預算**(§2):0 常駐 RAM、< 1MB plugin 本體、無輪詢 — 超標功能要么砍要么 P2
7. **零第三方依賴**:剝走 `python-gobject`、`dbus-python`、`requests`、`pyxdg`、`desktop-entry-lib`、`ftputil`;最終只靠 `python3` + `bsdtar`(皆預期系統已有);FTP update source 降級 P2(F13)

**2026-08-27(第三輪,Rust 評估):**
8. **Rust 唔採用**(§7.1.1):TLS stack 令 binary 5–15MB 打穿 < 1MB 預算、GearLever 邏輯要全重寫、發佈維護成本高;啟動/RAM 差異喺本場景感知唔到。Backend 收喺穩定 CLI `--json` 介面後面,將來要換隨時得,而家唔使買單

**2026-08-27(第四輪,MVP 實作期間):**
9. **bsdtar 假設被推翻(實測)**:libarchive 冇 squashfs reader → backend agent 自寫純 Python SquashFS 4.0 reader,對 neovim `--appimage-extract` ground truth 做 byte-for-byte 驗證(2057 檔全中);**最終運行時依賴收窄到淨 `python3`**
10. **File picker**:Quickshell 實測冇 FileDialog 類型(qmldir + qmltypes 搜證)→ MVP 用「路徑輸入框 + Downloads 快選列表」fallback;拖放(F11)照舊 P2
11. **MVP 交付**:見 §11「MVP 交付記錄」

---

## 11. 里程碑

| 階段 | 內容 | 驗收標準 |
|---|---|---|
| **M0 — Scaffold**(0.5 週) | repo、manifest.json、bar-widget + panel 骨架;**喺乾淨 Omarchy 機驗證 `python3`/`bsdtar` 存在** | `omarchy plugin validate` 過;qmllint 乾淨;summon/hide 正常 |
| **M1a — Backend 拆骨**(1–1.5 週) | 抄 GearLever 核心模組、剝走全部 gi/第三方 import(§7.4 手術表)、`bsdtar` 抽取、CLI-only 入口 | `--list-installed --json` 先通;再用官方 test files 跑通 `--integrate` / `--remove` / `--update`;喺乾淨環境跑證明零第三方依賴 |
| **M1b — MVP UI**(1 週) | F1–F5:list / integrate / remove / launch / bar widget | 三個用戶故事(§5)全部行到;缺 `python3` 時有清晰提示 |
| **M2 — Updates**(1 週) | F6–F9:update 檢查、一鍵 update、通知 service、custom source、頻率設定 | 有 update 時 badge + 通知正確;update 後列表同 app menu 一致 |
| **M3 — 發佈** | README、preview.png、marketplace 提交 | marketplace 接收;`omarchy plugin add <repo> --enable` 乾淨安裝;§2 輕量化預算全數達標 |
| **後續** | F10–F13 | — |

### MVP 交付記錄(2026-08-27,orchestrator 派 4 個 sub-agent:backend 拆骨 / QML scaffold / 整合 / 獨立驗收)

M0+M1a+M1b 經獨立驗收 agent 實證全數 PASS:55 個 backend unittest 全綠;AST import 審計零第三方;真 AppImage(neovim v0.11.3)完整 lifecycle(integrate → list → remove → Trash 三件套齊);`--json` stdout 純淨度驗證;`omarchy plugin validate` PASS;真機安裝 + enable + summon/hide + 截圖確認 panel 同 bar widget render、journal 零相關錯誤;靜止零 python 進程(0 常駐);總體積 312K;真 HOME 零污染。

Plugin 已裝並 enabled 喺開發機 `~/.config/omarchy/plugins/io.github.glasschan.appimage`。已知 P2(唔阻 MVP):`squashfs.py` 兩處 ResourceWarning(未閂 file handle);M3 發佈前要有排除 PRD/tests/`__pycache__` 嘅乾淨打包流程;滑鼠 click-through 未做自動化(證據 = 截圖 render + JS harness 35 項 + 代碼審閱)。

---

## 12. 成功指標

- MVP 後自用日常裝/卸 AppImage 完全經過 plugin(唔再開 GTK app)
- Marketplace 安裝數 + issue 反饋
- Panel 操作延遲感知 < 300ms(快取 hit)、backend call < 500ms
- **閒置額外 RAM = 0;plugin 本體 < 1MB;新增 pacman 依賴 = 0(理想)**

## 變更記錄

- **v0.5** — MVP 交付:bsdtar→純 Python SquashFS reader 修正(§7.3/§7.4);依賴最終 = 淨 `python3`;file picker fallback 決策(#10);新增 §11「MVP 交付記錄」
- **v0.4** — Rust 評估(§7.1.1):唔採用,記錄逐項比較 + escape hatch(CLI 介面保留將來換 backend 可能);決策記錄 #8
- **v0.3** — 加入 §2 輕量化硬預算;技術選型論證(§7.1);依賴清單收窄到 `python3` + `bsdtar`;剝走全部第三方庫;手術表更新(§7.4);M1a 估期加大;F13(FTP)降級 P2
- **v0.2** — 按第一輪決策定名、ID、backend 拆 CLI 方案、update 頻率 default
- **v0.1** — 初稿
