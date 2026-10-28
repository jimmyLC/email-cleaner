# Gmail 設定指南

這份指南帶你在 Google Cloud 建立一組只給自己用的 OAuth 憑證，並登入 Gmail。全程約 15 分鐘，不需要付費。畫面名稱以 Google 官方的 Gmail API 快速入門為準；Google 常改版，名稱若有出入，照意思找即可。

## 先知道

- Gmail 沒有資料夾，用**標籤**。收件匣就是 `INBOX` 標籤，封存就是把 `INBOX` 標籤拿掉，刪除是移到垃圾桶（保留 30 天，可還原）。
- 程式要求的權限：
  - 唯讀：`gmail.readonly`、`contacts.readonly`
  - 要整理時另外授權：`gmail.modify`。這個權限可以加減標籤和移到垃圾桶，**不能永久刪除，也不能寄信**。程式裡的寫入元件也只允許這四個動作：改標籤、移到垃圾桶、移出垃圾桶、建立標籤。
- 憑證只存在這台電腦的 `.env` 和 `data/gmail-token.json`，不會上傳。

## 步驟

### 1. 建立專案並啟用 API
1. 打開 Google Cloud Console（console.cloud.google.com），用要整理的 Gmail 帳號登入。
2. 建立一個新專案，名稱隨意，例如 `email-cleaner`。
3. 到「API 和服務」→「程式庫」，搜尋並啟用 **Gmail API**，再啟用 **People API**（用來讀通訊錄，保護你的聯絡人；不啟用也能用，只是少一層保護）。

### 2. 設定同意畫面（Google Auth platform）
1. 左側選單進入 **Google Auth platform**。若顯示「尚未設定」，按「開始使用」。
2. **品牌**：填應用程式名稱（例如 `email-cleaner`）和你的 Gmail 當支援信箱。
3. **目標對象（Audience）**：個人 Gmail 帳號請選 **外部（External）**。「內部」只給 Google Workspace 組織用。
4. 聯絡資訊填你的 Gmail，同意使用者資料政策，建立。

### 3. 發布成「正式版」（重要）
在「目標對象」頁面把發布狀態從「測試中」改成 **正式版（In production）**。
> 停在「測試中」時，Google 發的 refresh token **7 天就會失效**，每週都得重新登入。改成正式版就不會。
> 正式版但尚未通過 Google 驗證時，登入會看到「Google 尚未驗證這個應用程式」。這是自己建的應用程式的正常情況：按「進階」→「前往 email-cleaner（不安全）」即可。

### 4. 建立桌面應用程式的 OAuth 用戶端
1. Google Auth platform → **用戶端（Clients）** → **建立用戶端**。
2. 應用程式類型選 **電腦版應用程式（Desktop app）**，名稱隨意。
3. 建立後記下 **用戶端 ID** 和 **用戶端密鑰**。

### 5. 填進 `.env`
```sh
GMAIL_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
GMAIL_CLIENT_SECRET=xxxxxxxx
```
這兩個值不要貼到聊天或上傳到任何地方。填完執行 `chmod 600 .env`。

### 6. 登入（唯讀）
```sh
.venv/bin/email-cleaner login gmail
```
程式會印出一個網址，請在**這台電腦**的瀏覽器打開、登入、同意。Gmail 的授權不支援「裝置代碼」方式（Google 不允許 Gmail 權限用那種流程），所以必須在同一台電腦開瀏覽器。登入成功後，終端機會顯示完成。

## 之後的流程

```sh
.venv/bin/email-cleaner sync gmail            # 唯讀：建立索引（1 萬封約 15 到 20 分鐘）
.venv/bin/email-cleaner stats gmail
.venv/bin/email-cleaner groups gmail --limit 50
.venv/bin/email-cleaner plan gmail --groups 50   # 只預覽，不動信
.venv/bin/email-cleaner login gmail --write      # 要整理時才授權寫入
.venv/bin/email-cleaner apply gmail --groups 5 --yes   # 先小批試跑，到 Gmail 確認
.venv/bin/email-cleaner undo gmail BATCH_ID      # 整批復原
```

建議跟 Outlook 一樣的順序：先唯讀索引 → 看規則與預覽 → 小批試跑並到信箱確認 → 再整批跑。規則（寄件者、網域、網域加主旨）和 Outlook 共用，兩邊同一個寄件者會得到相同的處理；資料夾名稱會變成標籤，`封存` 一律是「只拿掉收件匣標籤」，不會建立同名標籤。

## 常見問題

- **登入後又要重新登入**：確認發布狀態是「正式版」。若曾改過權限範圍，刪除 `data/gmail-token.json` 後重新登入。
- **People API 沒啟用**：同步會略過通訊錄，只用「你寄過信的人」判斷客戶。
- **想撤銷權限**：到 Google 帳戶 → 安全性 → 第三方存取，移除 `email-cleaner`，並刪除 `data/gmail-token.json`。
- **配額**：Gmail API 每個使用者每秒有請求上限。程式遇到限流會依回應的等待時間自動重試，整理時也會逐封間隔，避免觸發。
