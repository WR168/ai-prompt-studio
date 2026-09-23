# 安全策略 (Security Policy)

## 适用范围

本策略适用于 `ai-prompt-studio`（主角一致 · AI 视频提示词工坊）项目的所有源码、配置、文档、CI 流程以及部署在 Streamlit Community Cloud 上的运行实例。

## 漏洞报告

发现安全漏洞请**不要**在 GitHub Issue 中公开披露。

- 私密报告：发送邮件到项目维护者邮箱（见仓库 Owner 主页）
- 响应时间：48 小时内确认收到，7 天内给出初步评估
- 修复时间：严重漏洞 7 天内、中高危 30 天内发布修复版本

报告时请包含：
- 漏洞描述与影响范围
- 复现步骤（脱敏，不要附带真实密钥）
- 建议的修复方向（可选）

## 密钥管理规范

### 绝不上传的文件

| 文件 | 说明 |
|------|------|
| `.streamlit/secrets.toml` | 真实密钥（API Key / Supabase Key / 密码）|
| `.env` `.env.*` | 环境变量文件 |
| `data/` | 本地 SQLite 数据库（含生成历史）|
| `output/` | 生成的提示词与 JSON 备份 |

以上文件已被 `.gitignore` 排除。**推送前请运行 `git status` 确认它们不出现在待提交列表中。**

### 可安全上传的配置

| 文件 | 说明 |
|------|------|
| `.streamlit/secrets.toml.example` | 密钥模板（仅占位符）|
| `.streamlit/config.toml` | Streamlit 平台配置（headless=true）|

### 密钥轮换流程

1. 在供应商控制台（智谱 / Supabase）生成新密钥
2. 更新本地 `.streamlit/secrets.toml`（或 Streamlit Cloud 的 Secrets 面板）
3. 删除旧密钥（供应商控制台 revoke）
4. 如果 `COOKIE_SIGN_KEY` 变更，所有用户的 Cookie 会失效（需重新登录）——这是预期行为

### Supabase 密钥权限要求

- **必须使用 `sb_publishable_` 开头的 anon 公钥**
- **严禁使用 `sb_secret_` 开头的 service_role 密钥**——这是高权限密钥，可在前端被提取
- 代码在 `app.py` 启动时会自动校验：发现 `sb_secret_` 前缀立即拒绝启动
- Supabase 表必须启用 RLS（Row Level Security），见 README.md 第二节的 SQL 脚本

### 限流表 RLS（部署必做项）

- `daily_quota` 表与其他业务表策略不同：anon 只授予 SELECT / INSERT，**UPDATE / DELETE 显式拒绝**（`using (false)`），防止绕过页面直接调 REST API 清零额度
- 自增操作通过 `incr_daily_quota(text, text)` 函数完成：`SECURITY DEFINER` + `search_path=public`，以表所有者身份原子 upsert，仅授予 anon EXECUTE
- 若只启用 RLS 而未创建函数，应用探测 RPC 404 后会降级到旧 upsert，在 UPDATE 禁令下限流计数会静默失效——**策略与函数必须同时部署**
- 其他四张表（characters / history / settings / user_profiles）当前使用 `for all to anon` 宽松策略，安全性依赖应用层密码门禁

### Cookie 签名

- 设备昵称 Cookie 使用 HMAC-SHA256 签名（`昵称.签名`），防止用户手改 Cookie 伪造他人身份
- 签名密钥 `COOKIE_SIGN_KEY` 从 `st.secrets` 读取，不硬编码
- **fail-closed**：未配置、不足 32 字符或仍是 `change-me` 占位符时，应用拒绝启动（`st.stop()`），不允许静默降级为不签名
- 生成命令：`python -c "import secrets; print(secrets.token_hex(32))"`

### Cookie 安全标志（实际设置值）

| 标志 | 值 | 说明 |
|------|-----|------|
| `Secure` | `True` | 仅 HTTPS 传输（云端全站 HTTPS；现代 Chrome/Edge/Firefox 对 `http://localhost` 也允许 Secure Cookie） |
| `SameSite` | `"Lax"` | 防 CSRF，跨站不带 Cookie |
| `path` | `"/"` | 全站生效 |
| `max_age` | 365 天 | 持久化登录 |
| `HttpOnly` | **无法设置** | `streamlit-cookies-controller` 0.0.4 不支持该参数——库通过前端 JS 组件读 Cookie，HttpOnly 会使其无法读取。Cookie 内容仅为 HMAC 签名后的昵称（无密码、无密钥），风险可接受；如未来需要 HttpOnly，须改用服务端 Session（如 JWT + `Set-Cookie` 中间件） |

## 认证与限流

### 访问密码

- `APP_PASSWORD` 使用 `hmac.compare_digest` 常量时间比较，防时序攻击
- 密码不存 Cookie；会话状态仅保存在 `session_state`
- 会话超过 `SESSION_MAX_HOURS`（默认 8 小时）自动登出
- 提供登出按钮，清除会话 + Cookie

### AI 调用限流

- 每日 AI 扩写次数限制（`DAILY_LIMIT`，默认 20 次/人/天）
- 限流计数存入服务端数据库（SQLite `daily_quota` 表 / Supabase `daily_quota` 表）
- 按"昵称 + 日期"唯一约束，**清 Cookie 换昵称无法绕过**（服务端计数跟着旧昵称）
- session_state 仅作缓存，服务端不可用时降级

## 错误处理

- 所有用户可见的错误消息经 `security_utils.safe_error()` 脱敏
- LLM / Supabase 响应体经 `security_utils.redact()` 脱敏，确保密钥不泄露
- 异常消息截断到 200 字符以内，避免长响应体泄露
- **全局异常兜底**：`app.py` 路由层捕获所有未处理异常，前端只显示 `safe_error(e)` 的一句话，不渲染 Streamlit 默认的完整 traceback。某些第三方库异常会把请求头 / 局部变量（可能含 `st.secrets` 的 Key、URL、密码）带进 traceback，全局兜底可避免它们直接出现在用户屏幕上；完整 traceback 仍写入服务端日志供运维排查（Streamlit Cloud 的日志面板仅应用维护者可见）
- `st.stop()` / `st.rerun()` 抛出的 `StopException` / `RerunException` 是流程控制信号，已显式放行不吞

## 部署前检查清单

```bash
# 0. 安装 pre-commit 钩子（第一道防线，仅首次）
pip install pre-commit
pre-commit install

# 1. 运行安全检查脚本
./scripts/security_check.ps1   # Windows
# 或 bash scripts/security_check.sh  # Linux/macOS

# 2. 确认 git 状态
git status --short
git ls-files | grep -iE 'secret|\.env|key|token|password'
# 确认输出为空

# 3. 运行测试
python _apptest_check.py
python _t_stream.py
python _security_test.py

# 4. 确认 .streamlit/secrets.toml 不在待提交列表
git status .streamlit/secrets.toml
# 应输出 "nothing to commit"

# 5. 静态检查：README 中必须存在 daily_quota 的 RLS 策略与 RPC 函数
grep -c "incr_daily_quota" README.md   # 应 ≥ 2（建函数 + grant）
```

## 纵深防御：两道密钥扫描防线

1. **本地 pre-commit（第一道）**：`.pre-commit-config.yaml` 在每次 `git commit` 前运行 gitleaks，密钥在进入本地提交前即被拦截；另有 trailing-whitespace / end-of-file-fixer / 大文件检查
2. **CI（第二道）**：`.github/workflows/security.yml` 在 push/PR 时全量扫描

## CI 安全扫描

`.github/workflows/security.yml` 在每次 push/PR 时自动执行：

1. **gitleaks**：全仓库密钥泄露扫描（含 Git 全历史，`--redact` 不回显真实密钥）
2. **pip-audit**：Python 依赖漏洞审计（基于锁定 patch 版本的 requirements.txt）
3. **bandit**：Python 静态安全分析

CI 失败会阻止合并到 main 分支。
