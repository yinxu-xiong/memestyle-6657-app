# DEPLOY.md — 部署到 Streamlit Community Cloud（任何人有链接即可玩，零登录）

> 应用从**公开仓库** `yinxu-xiong/memestyle-6657-app` 部署，观众无需登录。
> 原理：Streamlit Cloud 规则——私有仓库部署的应用观众必须登录才能看；公开仓库部署的应用任何人拿到链接直接玩。
> 红线：公网链接**只发粉丝群**，不要公开宣传；README 数据来源声明必须保留。

## 首次部署（约 10 分钟）

1. 打开 **https://share.streamlit.io/** → 右上角 **Sign in** → **Continue with GitHub** → 授权
2. **删除旧应用**（连私有仓库 `memestyle-6657` 的那个）：主页 "Your apps" 里找到它 → 应用卡片右侧 **"⋮"** 菜单（或进入应用后右下角 **Manage app**）→ **Settings** → 拉到最底 **Danger zone** → **Delete app** → 按提示确认删除
3. 回主页点 **New app**（或 Create app）
4. 表单填写：
   - **Repository**：下拉选 `yinxu-xiong/memestyle-6657-app`（公开仓库，无需任何私有授权）
   - **Branch**：`main`
   - **Main file path**：`app.py`
5. 点 **Advanced settings** → **Secrets** 框里粘贴这一行（无引号无空格）：

   ```
   DEEPSEEK_API_KEY=sk-你的key
   ```

   → Save
6. 点 **Deploy** → 等 2~5 分钟 → 得到形如 `https://xxx-app.streamlit.app` 的公网链接

## 部署后自查（逐条过）

- [ ] **零登录**：开一个无痕/隐私窗口（没登录过任何账号的）打开链接，能直接看到"🐽 6657 串子生成器"页面
- [ ] 输入一条弹幕（如 `比赛焦灼刺激玩机器激情解说时`）→ 10~30 秒出 3 张候选卡片
- [ ] 点一张卡片的 **"就用这条"** → 按钮变灰、显示"已记录：选了第 X 条"
- [ ] **确认公开**：应用页面右下角 **"⋮"** → **Settings** → Sharing/Access → 选 **"Anyone with the link"**（公开仓库部署默认即是，看一眼确认）
- [ ] **每日 300 次限额**：代码里 `DAILY_LIMIT = 300`（app.py）随部署自动生效；用完显示"今日额度用完，明天再来 🐷"
- 首次打开 10~30 秒慢 = 免费版休眠"睡醒"，正常

## 最终验收（手机）

手机**关 WiFi 用流量**打开链接 → 不登录直接能玩 → 三选一生成成功。

## 更新代码

clone 本仓库 → 覆盖改动的文件 → commit → push → Streamlit Cloud 自动重新部署（约 2 分钟）。

## 日常维护

- **看日志**：share.streamlit.io → 应用 → Manage app，最下面就是运行日志
- **改 Secrets**：应用右下角 "⋮" → Settings → Secrets → 改完 Save → Restart
- **重启应用**：应用右下角 "⋮" → Restart
- 链接只发粉丝群
