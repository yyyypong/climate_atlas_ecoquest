# Ecoquest · Climate Atlas · 欧洲气候远征

一个气候政策教学桌游（网页版）。玩家代表欧洲 8 国之一，在 6 或 18 回合内推进能源转型与生态保护，通过建设、谈判与国家行动达成共同目标，结束时按 7 项加权指标（满分 100）排名。

游戏内嵌 AI 能力三个：**AI 对手**（控制电脑代表团）、**决策教练**（回合内给教学反馈）、**整局复盘**（赛后总结与可落地的校园行动建议）。

技术上是**单文件 HTML + 本地 Python 服务**，只用 Python 标准库，无第三方依赖。

> 演示实例：https://ecoquest-climate-atlas.vercel.app

---

## 快速开始

### 只玩，不需要 API（最快）

直接双击 `Ecoquest-Climate-Atlas.html`。全部游戏功能可用，新闻显示随附的、标注日期的真实报道快照。

AI 对手会自动切换为**规则对手**（按效用函数选最优），决策教练与整局复盘不生成。游戏流程与计分完全不受影响。

### 本地完整运行（含 AI）

1. 需要 Python 3（无第三方依赖）。
2. 配置密钥，见下一节。
3. 双击 `启动Ecoquest-实时新闻.cmd`，浏览器会自动打开。

### 云端（无需安装）

访问 https://ecoquest-climate-atlas.vercel.app 。这是已配置好密钥的演示实例，AI 功能开箱可用。

---

## API 密钥怎么配

**密钥绝不写进源码，也绝不会出现在浏览器里。** 服务端只在转发请求时使用它，浏览器从头到尾看不到。

优先级：**环境变量 > 配置文件**。

### 环境变量（部署场景推荐）

```bash
export DEEPSEEK_API_KEY=sk-你的密钥
export DEEPSEEK_BASE_URL=https://api.deepseek.com
export DEEPSEEK_MODEL=deepseek-chat
```

Windows PowerShell：

```powershell
$env:DEEPSEEK_API_KEY = "sk-你的密钥"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-chat"
```

### 配置文件（本地场景）

复制 `.env.example` 为 `18.env`，填入自己的值：

```
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

`18.env` 与 `.env` 已被 `.gitignore` 忽略，不会被提交。

### 兼容性

任何 OpenAI 兼容的接口都可以，只要提供上面三个值：DeepSeek、OpenAI、Moonshot、通义千问，或者本地 Ollama / vLLM。

配置在**每次模型请求时重新读取**，所以改完密钥无需重启服务。

---

## 目录结构

```
.
├── Ecoquest-Climate-Atlas.html      游戏本体（单文件，含全部界面/逻辑/知识库）
├── serve_game.py                     本地服务：新闻抓取 + 模型代理 + 静态托管
├── start-ecoquest.ps1                启动脚本（自动寻找 Python）
├── 启动Ecoquest-实时新闻.cmd          Windows 一键启动
├── news-snapshot.json                离线新闻快照（断网时使用）
├── .env.example                      配置模板（复制为 18.env 或 .env 填写）
├── .gitignore
├── Ecoquest-身份与新闻说明.md         游戏玩法说明
├── Ecoquest-骰子与排名改版说明.md      界面改版说明
└── vercel_app/                       云端部署版本（可选，见下）
```

`vercel_app/` 结构：

```
vercel_app/
├── api/                  Vercel Serverless Functions
│   ├── _ecoquest.py      模型与新闻核心库（与本地版同源）
│   ├── health.py         /api/health 与 /api/ai/status
│   ├── news.py           /api/news
│   └── ai/               opponent / coach / review / status
├── public/               静态资源（index.html、新闻快照）
└── vercel.json
```

---

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/ai/status` | 模型服务状态（会真实发一个最小请求验证密钥，而不是只读配置文件） |
| GET | `/api/news?lang=zh\|en` | 联合国新闻（15 分钟刷新，失败自动回退缓存） |
| POST | `/api/ai/opponent` | AI 对手：返回一个事件选项 id + 一个行动选项 id + 理由 |
| POST | `/api/ai/coach` | 决策教练：四维度打分 + 教学反馈 + 引用条目 id |
| POST | `/api/ai/review` | 整局复盘：总结、关键决策、校园行动建议 |

### AI 的行为约束

这些约束同时写在代码和系统提示里：

- AI **只控制电脑代表团**，永远不替玩家行动。
- AI **从不修改游戏数值**。它只从候选 id 里挑，然后由游戏引擎执行。
- 只能引用请求中提供的条目 id，**不能编造**来源、链接、数据。引用 id 会做白名单过滤，编造的一律丢弃。
- 游戏内所有预算、能源、生态数字都是**教学单位**，不代表真实成本或减排数据。
- 教练与复盘的分数来自固定 rubric（目标契合度、资源取舍、长期韧性、跨国合作，各 0–5），与游戏内成绩**分开显示**。

---

## 降级行为（没有密钥也能完整玩）

| 情况 | 表现 |
| --- | --- |
| 未配置密钥 | AI 对手切换为规则对手，游戏照常进行 |
| 密钥无效 / 额度不足 / 网络故障 | 界面显示具体原因，本回合按规则推进 |
| 模型返回无法解析 | 该回合按规则策略执行 |
| 新闻获取失败 | 保留上次成功的内容并标注「更新失败」，或回退到随附快照 |

---

## 部署到 Vercel

```bash
cd vercel_app
vercel deploy --prod
```

在 Vercel 项目的 **Environment Variables** 里设置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 三个变量。

`api/` 目录下的文件会被 Vercel 当作 Serverless Functions，`public/` 目录作为静态资源（见 `vercel.json` 的 `outputDirectory`）。

本地可先跑 `python vercel_app/smoke_test.py` 验证各 handler。

---

## 安全说明

- 本仓库**不含任何密钥**。`.gitignore` 忽略 `18.env`、`.env` 与所有凭据文件。
- 服务端只监听 `127.0.0.1`（本地版），且拒绝非本机来源的请求。
- 上游 API 密钥只在服务端转发给模型提供方，不下发浏览器。
- 演示实例的密钥存放在 Vercel 平台的环境变量中，不在代码里。

---

## 游戏玩法摘要

- 可选国家：德国、荷兰、葡萄牙、波兰、法国、西班牙、丹麦、瑞典。初始资源相同，目标与国家行动不同。
- 回合循环：掷 3D 骰子前进 1–6 站 → 领取 8 预算与设施收益 → 处理目的地城市事件 → 执行一次行动（建设 / 手牌 / 跨国合作 / 国家行动，四选一）。
- 气候议会、Action Lab、跨国谈判、卡牌档案、真实新闻插播、策略顾问、整局手记。
- 终局综合排名（满分 100）：能源进度 22 + 生态进度 22 + 生态净贡献 18 + 设施建设 12 + 长期韧性 8 + 跨国合作 8 + 预算余量 5 + 双目标达成 5。
- 集体胜利条件不变：共同生态达标，且至少半数国家完成双目标。排名只用于比较各国表现。

详见 `Ecoquest-身份与新闻说明.md`。

---

## 数据来源与致谢

- 新闻：[联合国新闻 · 气候与环境](https://news.un.org/zh/news/topic/climate-change)（中文 / [English](https://news.un.org/en/news/topic/climate-change)）
- 政策与规则条目引用欧盟委员会、EUR-Lex、HELCOM 等公开来源，条目 id 与链接可在游戏内点击查看
- 界面修整方法来源：[unslop](https://heydamir.design/unslop)，作者 Damir Sokolovsky（[@heydamir](https://x.com/heydamir)），CC BY 4.0
- 原地图与插画保留
