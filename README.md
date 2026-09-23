# 🎬 主角一致 · AI 视频提示词工坊

输入一句话创意（如"小猫走路去上课"），自动扩写成带**分镜、运镜、光线、时间轴、负面提示词**的完整 AI 视频提示词，面向可灵 Kling / 即梦 Dreamina / Runway / Sora 等文生视频工具。

## ✨ 功能总览

- 🐱 **主角一致性引擎（角色卡）**：把主角外观固定成"锚定描述"，每条提示词原样注入，跨视频、跨镜头保持同一形象
- 👥 **多角色同框**：最多 3 位主角同框出演。AI 被强制为**每位角色、每个镜头**写 80-100 词独立外观描述，严禁压缩与"如上"偷懒；合并负面提示词自动附带防融合词条（`character fusion, mixed features, merged bodies, color bleeding`）；云端后台逐镜校验字数，不达标自动点名重试
- 🈶 **中英对照 + ComfyUI 优化**：结果拆成 4 个独立代码块——中文正面、英文正面、中文负面、英文负面，各自带一键复制按钮，直接粘贴进 ComfyUI / 视频工具
- 🎬 **分镜连贯性**：AI 强制按"起势→发展→高潮→收尾"推进剧情，景别按"全景→中景→特写→全景"切换，每镜标注时间轴（如 `时间：0-4秒`），可直接串成 10-20 秒连贯视频
- 🎲 **本地模板生成**：免费、离线，模板拼接即刻出稿；✨ **AI 智能扩写**兼容所有 OpenAI 格式接口（智谱 GLM / DeepSeek / 硅基流动 / 通义千问 / Kimi / Ollama 等）
- 👤 **设备昵称与头像（Cookie 自动登录）**：首次打开填昵称、传头像并点「💾 保存我的配置」，档案存入 Supabase、昵称写入浏览器 Cookie（365 天，受浏览器单 Cookie 4KB 限制，头像不进 Cookie、按昵称自动从云端加载）；此后在同一浏览器打开网页自动恢复身份、自动显示头像，无需登录、无需书签。朋友用各自的浏览器打开即自动拥有独立身份。历史记录和角色档案带彩色创建者徽章与头像，一眼区分谁生成的
- 🔍 **历史管理**：关键词模糊搜索、按设备筛选、单条删除（带二次确认）、JSON 批量备份 + TXT 导出
- 🔒 **密码门禁**：部署到公网后防止陌生人白嫖；⏱ **每日额度**：每人每日 AI 扩写次数可配（默认 20），侧边栏进度条实时显示
- 📱 **手机自适应**：手机浏览器直接打开即用，无需安装任何 App

---

## 目录

- [一、本地启动指南](#一本地启动指南)
- [二、云端数据库配置（Supabase）](#二云端数据库配置supabase)
- [三、云端 Secrets 配置](#三云端-secrets-配置)
- [四、部署到公网：Streamlit Community Cloud（保姆级）](#四部署到公网streamlit-community-cloud保姆级)
- [五、常见问题排查](#五常见问题排查)
- [六、免费版限制须知](#六免费版限制须知)
- [项目结构](#项目结构)

---

## 一、本地启动指南

需要 Python 3.10+。

```bash
# 1. 进入项目目录
cd ai-prompt-studio

# 2. 创建并激活虚拟环境（首次）
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动
streamlit run app.py
```

Windows 用户也可以直接双击桌面快捷方式「AI视频提示词工坊」（或运行项目里的 `启动.bat`，已改为相对路径，拷贝整个项目文件夹后仍可直接使用）。

### 安装提交前安全钩子（推荐）

本仓库自带 `.pre-commit-config.yaml`，在 `git commit` 前自动扫描密钥（gitleaks，第一道防线）并整理空白字符：

```bash
pip install pre-commit
pre-commit install          # 仅首次需要，安装后每次 commit 自动执行
pre-commit run --all-files  # 手动对全量文件跑一次
```

> 第二道防线是 GitHub Actions 中的 CI 扫描（`.github/workflows/security.yml`，含 gitleaks / pip-audit / bandit）。

### 本地密钥配置

1. 复制密钥模板：

   ```bash
   copy .streamlit\secrets.toml.example .streamlit\secrets.toml
   ```

2. 打开 `.streamlit/secrets.toml`，**至少填入 `COOKIE_SIGN_KEY`（必填项，本地开发也需要）**：

   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```

   把输出的 64 位随机串粘贴到 `COOKIE_SIGN_KEY`；其余字段（智谱 API Key、Supabase、访问密码）按需填写（完整字段说明见[第三节](#三云端-secrets-配置)）。
3. **`secrets.toml` 已被 `.gitignore` 排除，永远不会上传 GitHub**。

> 最小配置原则：本地使用只需 `COOKIE_SIGN_KEY` 一项即可启动——密码门禁自动关闭（`APP_PASSWORD=""`），AI 接口 Key 可在应用内【⚙️ 设置】页填写，数据自动回退本地 SQLite（`data/app.db`，自动建表）。`COOKIE_SIGN_KEY` 不可省略：缺失时应用拒绝启动（fail-closed 设计，防止部署到公网后忘记配置）。

---

## 二、云端数据库配置（Supabase）

Supabase 免费版即可。配置后主角档案 / 生成历史 / 设置全部存云端数据库，应用重启、换设备都不丢。

### 第 1 步：创建项目

1. 注册登录 https://supabase.com ，点 **New project** 创建一个项目（名字随意，区域选离你近的，如 Singapore）。
2. 记下数据库密码（后面用不到，但别泄露）。

### 第 2 步：建表（三张表）

打开 Supabase Dashboard → 左侧 **SQL Editor** → **New query**，粘贴运行以下 SQL：

```sql
-- 主角档案（角色卡）
create table if not exists characters (
  id bigint generated always as identity primary key,
  name text not null unique,
  anchor text not null,
  style text not null default '',
  negative text not null default '',
  seed text not null default '',
  reference_note text not null default '',
  creator_name text not null default '',
  created_at text not null,
  updated_at text not null
);

-- 生成历史
create table if not exists history (
  id bigint generated always as identity primary key,
  character_id bigint references characters(id) on delete set null,
  character_name text not null,
  user_input text not null,
  mode text not null,
  model text not null default '',
  result text not null,
  creator_name text not null default '',
  created_at text not null
);

-- LLM 设置
create table if not exists settings (
  key text primary key,
  value text not null
);

-- 用户档案（设备昵称 + Base64 头像持久化，支持浏览器 Cookie 自动登录）
create table if not exists user_profiles (
  id bigint generated always as identity primary key,
  nickname text not null unique,
  avatar_base64 text not null default '',
  created_at text not null
);

-- 每日 AI 调用限流（按"昵称 + 日期"计数，服务端存储防清 Cookie 绕过）
create table if not exists daily_quota (
  nickname   text not null,
  quota_date text not null,
  count      bigint not null default 0,
  primary key (nickname, quota_date)
);
```

### 已有旧表？补一列即可

如果之前已经按老版本 README 建过表（缺 `creator_name` 列、没有 `user_profiles` 表），在 SQL Editor 里运行（全部语句都幂等，重复执行无副作用）：

```sql
alter table characters add column if not exists creator_name text not null default '';
alter table history    add column if not exists creator_name text not null default '';

create table if not exists user_profiles (
  id bigint generated always as identity primary key,
  nickname text not null unique,
  avatar_base64 text not null default '',
  created_at text not null
);
alter table user_profiles enable row level security;
create policy "anon full user_profiles" on user_profiles for all to anon
  using (true) with check (true);

-- 限流表（v3 新增；防清 Cookie 绕过每日额度）
create table if not exists daily_quota (
  nickname   text not null,
  quota_date text not null,
  count      bigint not null default 0,
  primary key (nickname, quota_date)
);
```

> 本地 SQLite 后端无需任何操作：应用启动时自动检测并补列。

### 第 3 步：放行 RLS（必做，否则写入报 permission denied）

Publishable key 以"匿名角色（anon）"身份访问，默认会被行级安全（RLS）拦截。继续在 SQL Editor 运行：

```sql
alter table characters enable row level security;
alter table history   enable row level security;
alter table settings  enable row level security;
alter table user_profiles enable row level security;

create policy "anon full characters" on characters for all to anon
  using (true) with check (true);
create policy "anon full history" on history for all to anon
  using (true) with check (true);
create policy "anon full settings" on settings for all to anon
  using (true) with check (true);
create policy "anon full user_profiles" on user_profiles for all to anon
  using (true) with check (true);

-- ===== daily_quota 限流表：最小权限（与其他表不同！）=====
-- 只允许 anon 读取和首次插入；禁止直接 UPDATE / DELETE，
-- 防止有人绕过页面调 REST API 把计数清零。
alter table daily_quota enable row level security;

create policy "anon select daily_quota" on daily_quota
  for select to anon using (true);
create policy "anon insert daily_quota" on daily_quota
  for insert to anon with check (true);
create policy "no_anon_update daily_quota" on daily_quota
  for update to anon using (false);
create policy "no_anon_delete daily_quota" on daily_quota
  for delete to anon using (false);

-- 原子自增函数：以表所有者身份（SECURITY DEFINER）执行 upsert，
-- 绕过上面的 UPDATE 禁令；anon 只有 EXECUTE 权限，无法直接改计数。
create or replace function incr_daily_quota(p_nickname text, p_quota_date text)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  new_count integer;
begin
  insert into daily_quota(nickname, quota_date, count)
  values (p_nickname, p_quota_date, 1)
  on conflict (nickname, quota_date)
  do update set count = daily_quota.count + 1
  returning count into new_count;
  return new_count;
end;
$$;

grant execute on function incr_daily_quota(text, text) to anon;
```

> ⚠️ **限流表 RLS 是部署必做项**：若不执行上面的 `daily_quota` 策略与 `incr_daily_quota` 函数，任何人拿到 Publishable key 后都能直接 `PATCH/DELETE /rest/v1/daily_quota` 把额度清零。应用会自动探测 RPC：函数不存在时降级为旧写法（需要 UPDATE 权限），收紧 RLS 后必须同时部署该函数，否则限流计数会静默失效（页面仍可用，仅额度不增长）。
>
> 取舍说明：characters/history/settings/user_profiles 四张表允许匿名角色完全读写，安全性由应用层的密码门禁承担——这是朋友圈小工具的合理取舍。若担心有人绕过页面直接调 REST API，可参照 daily_quota 的方式收紧策略；service_role key 绝不能放进前端 Secrets。

### 第 4 步：拿到两个配置值

Dashboard → **Project Settings → API**：

| 配置项 | 在哪找 | 长什么样 |
|---|---|---|
| `SUPABASE_URL` | **Project URL** | `https://xxxx.supabase.co`（带不带 `/rest/v1` 后缀均可，代码自动处理） |
| `SUPABASE_KEY` | **Publishable key** | `sb_publishable_` 开头（旧版项目为 anon public key，`eyJ...` 开头） |

---

## 三、云端 Secrets 配置

API Key 和访问密码**绝对不要写进代码**，统一走 Secrets（云端在 Streamlit 后台网页配置，本地在 `.streamlit/secrets.toml`）。TOML 格式如下：

```toml
# ===== 网页访问密码（防白嫖）。留空 "" = 不启用门禁 =====
APP_PASSWORD = "发给朋友的访问密码"

# ===== 每人每日 AI 扩写次数上限（本地模板不受限）=====
DAILY_LIMIT = 20

# ===== 【必填】Cookie 签名密钥，至少 32 字符；不配应用拒绝启动 =====
# 生成：python -c "import secrets; print(secrets.token_hex(32))"
COOKIE_SIGN_KEY = "用上面命令生成的 64 位十六进制随机串"

# 会话过期时间（小时），超时自动登出。默认 8
SESSION_MAX_HOURS = 8

# ===== AI 接口（OpenAI 兼容格式，此处以智谱 GLM 为例）=====
[llm]
API_BASE = "https://open.bigmodel.cn/api/paas/v4"
API_KEY = "你的智谱 APIKey（形如 xxxxxxxx.yyyyyyyy）"
# 主模型：glm-4.5-air 严格执行长文本/结构化指令；能力递增 glm-4-flash < glm-4-air < glm-4.5-air < glm-4-plus
MODEL = "glm-4.5-air"
# 兜底模型：主模型失败（限流/网络抖动）时自动切换重试一次
MODEL_FALLBACK = "glm-4.7-flash"

# ===== 云端数据库（不配置则回退本地 SQLite）=====
[supabase]
SUPABASE_URL = "https://你的项目ID.supabase.co"
SUPABASE_KEY = "sb_publishable_xxxxxxxxxxxx"
```

字段说明：

- `APP_PASSWORD`：网页访问密码。**留空 `""` 则不启用门禁**（任何人可访问，仅建议自用测试）。
- `DAILY_LIMIT`：每人每日 AI 扩写次数上限（默认 20），侧边栏实时显示剩余次数。
- `COOKIE_SIGN_KEY`：**必填项**。用于 HMAC 签名设备昵称 Cookie，防止用户手改 Cookie 伪造身份。缺失、不足 32 字符或仍是 `change-me` 占位符时，应用会拒绝启动（fail-closed）。用 `python -c "import secrets; print(secrets.token_hex(32))"` 生成；更换后所有设备需重新设置昵称。
- `SESSION_MAX_HOURS`：登录会话最长有效时间（默认 8 小时），超时自动登出。
- `[llm]`：配置后所有访客共用这一个 Key，应用内【设置】页输入框自动锁定并提示"由云端 Secrets 提供"，Key 不暴露给访客。换 DeepSeek / 硅基流动 / 通义千问等只需改这三行。`MODEL_FALLBACK` 可留空。
- `[supabase]`：对应第二节的 Project URL 和 Publishable key；只接受 `sb_publishable_` 开头的 anon 公钥，填入 service_role key 会被应用拒绝启动。不配置则数据存容器内 SQLite（云端重启会丢，见第六节）。
- 优先级：Secrets > 设置页存入数据库的值 > 内置默认值。

智谱 API Key 获取：https://open.bigmodel.cn → 控制台 → API Keys。

---

## 四、部署到公网：Streamlit Community Cloud（保姆级）

Streamlit Community Cloud 免费托管，部署成功后得到 `https://xxx.streamlit.app` 网址，朋友手机/电脑浏览器点开即用。

### 第 0 步：准备账号

- **GitHub** 账号（免费注册：https://github.com）
- **Streamlit** 账号（用 GitHub 一键登录：https://share.streamlit.io）

### 第 1 步：推送代码到 GitHub

命令行方式（推荐）：

```bash
cd ai-prompt-studio
git init
git add .
git commit -m "first commit"
git branch -M main
git remote add origin https://github.com/你的用户名/ai-prompt-studio.git
git push -u origin main
```

或者用 **GitHub Desktop**（无需命令行）：File → Add local repository 选择本项目 → Commit → Publish repository。

推送前自查清单（本项目已全部配好）：

- [x] `.gitignore` 已排除 `.streamlit/secrets.toml`（真实密钥）、`data/`（本地数据库）、`output/`、`.venv/`、`__pycache__/`、`*.log`
- [x] 已安装 pre-commit 钩子（gitleaks 在提交前扫描密钥）
- [x] `git status` 确认 `.streamlit/secrets.toml` 不在待提交列表
- [ ] 推送后到 GitHub 网页确认仓库里**没有** `secrets.toml`、没有 `data/app.db`

> 私有仓库（Keep this code private）也能免费部署 Streamlit，建议私有。

### 第 2 步：在 Streamlit Cloud 部署

1. 打开 https://share.streamlit.io ，用 GitHub 账号登录。
2. 点 **Create app → Deploy a public app from GitHub**，填写：
   - **Repository**：`你的用户名/ai-prompt-studio`
   - **Branch**：`main`
   - **Main file path**：`app.py`
   - **App URL**（可选）：自定义子域名，如 `ai-prompt-studio`
3. 点 **Deploy!**，等待 1-3 分钟，日志出现 `You can now view your Streamlit app in your browser.` 即成功。

### 第 3 步：配置云端 Secrets（部署后立即做）

1. 在 share.streamlit.io 找到你的应用 → 右下角 **⋮（三个点）→ Settings**。
2. 左侧选 **Secrets**，把[第三节](#三云端-secrets-配置)的 TOML 内容粘贴进去，改成真实值，点 **Save**。
3. 应用自动重启，密码门禁、AI 扩写、云端数据库立即生效。

### 第 4 步：以后更新代码

代码改完后 commit + push，Streamlit 云端检测到更新会**自动重新部署**；也可在应用菜单里手动 **Reboot app**。

---

## 五、常见问题排查

### 1. 本地双击启动后浏览器显示 `localhost 拒绝连接 / ERR_CONNECTION_REFUSED`

服务没起来。排查顺序：

1. **看 CMD 窗口是否停在 `Email:` 提示**：Streamlit 首次运行会询问邮箱，卡住后服务不启动。本项目已用 `.streamlit/config.toml` 的 `headless = true` 永久跳过；若仍出现，按回车留空即可。
2. **看 CMD 红色报错**（如 `ModuleNotFoundError`）：缺依赖，激活虚拟环境后 `pip install -r requirements.txt`。
3. **端口 8501 被占用**：`netstat -ano | findstr :8501` 找进程号，`taskkill /PID <进程号> /F` 结束；或换端口 `streamlit run app.py --server.port 8502`。

### 2. 云端部署后应用报红 / 一直转圈

- 在 share.streamlit.io 打开应用 **⋮ → Logs** 看错误。
- 最常见 `ModuleNotFoundError`：确认 `requirements.txt` 里有该库。
- 确认入口文件是仓库根目录的 `app.py`。

### 3. AI 扩写报"AI 正在休息"或连接超时

- 检查 `API_BASE` 是否正确（程序自动补 `/chat/completions`）。
- 云端容器访问国内 API 偶有网络波动，多重试一次；确认 Key 有余额、模型名正确。
- 用【⚙️ 设置 → 🔌 测试当前连接】快速定位。

### 4. Supabase 写入报错

| 报错关键字 | 原因 | 解决 |
|---|---|---|
| `relation "characters" does not exist` | 表还没建 | 跑第二节的建表 SQL |
| `relation "user_profiles" does not exist` | 用户档案表还没建 | 跑第二节的建表 SQL（或"旧表补一列"小节） |
| `permission denied` / `row-level security` / HTTP 401·403 | RLS 拦截匿名角色 | 跑第二节的 `create policy ... to anon` |
| `column ... creator_name ...` / PGRST204 | 旧表缺新列 | 跑第二节的 `alter table ... add column` |
| `Invalid API key` | KEY 填错 | 必须是 `sb_publishable_`（或旧版 anon key），不是 service_role |
| `无法连接 Supabase` | URL 填错或网络不通 | 核对 Project URL |

### 5. 忘了密码

到 Streamlit 后台 **Secrets** 里改 `APP_PASSWORD`，Save 后即生效。

---

## 六、免费版限制须知

Streamlit Community Cloud 免费，小圈子使用完全够用，但注意：

- **文件系统不持久**：应用重启/重新部署/休眠后容器内文件清空。**配置 `[supabase]` 后数据存云端数据库不受影响**；未配置时数据存容器内 SQLite 会丢，请定期在【🗂 生成历史】点"导出筛选结果（JSON 备份）"。
- **休眠机制**：长时间无人访问会休眠，再次打开需等几十秒冷启动。
- **资源上限**：单应用内存/运行时长有限，不适合高并发。
- 所有访客共享同一份云端数据和同一个 API Key（Secrets 配置），适合朋友小范围使用。
- 头像存放在 Supabase `user_profiles` 表（Base64，≤2MB），浏览器 Cookie 只保存昵称令牌（365 天）；清除浏览器 Cookie 后需重新保存一次配置。

---

## 项目结构

```
ai-prompt-studio/
├── app.py                  # Streamlit 入口：4 个页面 + 密码门禁 + 防刷额度 + CSS 美化 + 头像/昵称
├── prompts.py              # 本地模板引擎 + LLM 消息构造 + 多角色校验（纯函数，可单测）
├── llm.py                  # OpenAI 兼容接口调用（流式 SSE + 兜底模型重试）
├── storage.py              # 存储层：Supabase 云端 / 本地 SQLite 双后端 + 文件导出
├── requirements.txt        # 云端依赖清单（锁定大版本）
├── 启动.bat                 # Windows 一键启动（本机用，云端忽略）
└── .streamlit/
    ├── config.toml         # 平台配置（可上传）：跳过邮箱引导、关闭统计
    ├── secrets.toml        # 本地密钥（已 gitignore，永不上传）
    └── secrets.toml.example# 密钥模板（随仓库分发，供参考）
```
