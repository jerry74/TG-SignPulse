# TG-SignPlus v3 部署與回滾

正式服務只接受 `ghcr.io/jerry74/tg-signplus:sha-<12>` 固定標籤。部署目錄為
`/opt/docker/stacks/tg-signplus`，資料只寫入其 `data/` 子目錄。

## 預部署

1. 從 Bitwarden 注入固定的 `APP_MASTER_KEY`、管理員密碼與 Telegram API 參數。
2. 將 `.env` 設為 `0600`，將 `data/` 所有者設為容器 UID/GID `10001`。
3. 執行 `docker compose config --quiet`、`pull`、`up -d`。
4. 驗證 `/healthz`、`/readyz`、首頁、容器日誌與實際 image digest。
5. 以唯讀舊資料副本執行 `python -m signplus.import_cli --dry-run`；正式套用仍保持任務停用。

## 切換

先在 NAS 保存設定與 inspect 摘要，接著只執行
`docker compose stop app ai-solver`。不得執行 `down`、`rm` 或刪除 volume。舊服務停止後，
新版先完成 Saved Messages 往返及正式 chat 的唯讀預檢，才能啟用單一 canary。

## 回滾

正式 Bot 尚未執行時，停止新版，再於 NAS 原目錄執行
`docker compose start ai-solver app`。若 canary 已執行，先停用新版任務與容器，等舊排程
當日時間窗結束後才恢復 NAS，避免重複簽到。
