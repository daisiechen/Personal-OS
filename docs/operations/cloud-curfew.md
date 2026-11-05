# 云端会话的 11 点收工钩子（2026-11-04）

## 为什么要做
晚上 11 点后她给 Claude Code 发新指令会被挡回（她 2026-10-31 定，规则见 [devices.md](devices.md)“工作狂指数和晚上收工”）。
Mac 上装在 `~/.claude/settings.json`，所有会话都生效；云端会话原来只靠仓库里的 `.claude/settings.json`。
2026-11-04 统一巡检查出漏洞：**云端会话常从上层目录 `/home/user` 开（一个会话挂好几个仓库），读不到仓库里的设置，钩子不生效**，11 点后照样能发指令。

办法：在 claude.ai 云端环境的“启动脚本”（Setup script）里加一段，每次开环境时把收工钩子写进 `~/.claude/settings.json`（用户级，从哪个目录开都读）。

- 脚本里**不放任何钥匙**。钩子运行时才读环境里已有的 `ETHAN_CLOUD_TOKEN`（启动脚本本身读不到环境变量和密钥，也不需要）。
- 定时任务唤醒会话的消息和“定时巡检”照样放行（2026-11-02 她授权），巡检夜里不受影响。
- 连不上服务器就放行，不会把会话卡住；脚本出错也不影响环境启动（`|| true`）。
- 和仓库里那份同时生效时只是多问一次服务器，结果一样。

## 她要做的（一次，约 2 分钟）
1. 打开 claude.ai/code，新建会话页输入框下面第一个按钮（环境选择器），鼠标移到正在用的环境（Default）那一行，点右边的齿轮。
2. 找到 **Setup script**，在原有内容的**最后**粘贴下面整段（原来是空的就直接粘），保存。
3. 新开一个云端会话才会生效。验收：新会话里跑 `python3 -m ethan.setup_hook --check`，第一行应是“…/.claude/settings.json：有收工钩子（只在云端会话生效）”。

要粘贴的整段（也可以在仓库里跑 `python3 -m ethan.setup_hook --where cloud-user` 生成，内容一样）：

```bash
# 小伴 收工钩子（python3 -m ethan.setup_hook --where cloud-user 生成，docs/operations/cloud-curfew.md）
# 11 点后她发的新指令挡回；定时任务唤醒和“定时巡检”放行。不含钥匙，钩子运行时读 ETHAN_CLOUD_TOKEN。
mkdir -p "$HOME/.claude"
python3 - <<'ETHAN_CURFEW_PY' || true
import json, os
p = os.path.expanduser("~/.claude/settings.json")
try:
    s = json.load(open(p, encoding="utf-8"))
except Exception:
    s = {}
cmd = "[ \"$CLAUDE_CODE_REMOTE\" = true ] || exit 0; case \"$(cat)\" in *queued-remote-notifications*|*定时巡检*) exit 0;; esac; t=\"${ETHAN_HOOK_TOKEN:-$ETHAN_CLOUD_TOKEN}\"; [ -n \"$t\" ] || exit 0; r=$(curl -s -m 4 -A ethan-curfew -H \"Authorization: Bearer $t\" \"https://ethan.example.com/cloud/curfew\") || exit 0; printf '%s' \"$r\" | python3 -c 'import json,sys\ntry:\n d=json.load(sys.stdin)\nexcept Exception:\n sys.exit(0)\nif d.get(\"stop\"):\n print(d.get(\"message\") or \"已经 11 点了，今天到这儿。\", file=sys.stderr); sys.exit(2)'"
hooks = s.setdefault("hooks", {})
groups = [g for g in hooks.get("UserPromptSubmit", []) if not any("ethan-curfew" in h.get("command", "") for h in g.get("hooks", []))]
groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 8}]})
hooks["UserPromptSubmit"] = groups
json.dump(s, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("小伴 收工钩子已写进", p)
ETHAN_CURFEW_PY
```

改了钩子（`ethan/setup_hook.py` 的 `curfew_command`）以后，要按新生成的内容重贴一次；`tests/test_syscheck.py` 查仓库设置和生成的一致、脚本重复跑只留一份、不含钥匙。

## 服务器会话怎么查
服务器上的会话（洛杉矶 `claude remote-control`，worktree 模式）读的是 ethan 用户的 `~/.claude/settings.json` 和仓库副本里的 `.claude/settings.json`。在她 Mac 上跑：

```bash
ssh my-server "sudo -u ethan -H bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_hook --check --repo .'"
```

会列出两个文件里有没有收工钩子、钩子是不是只在云端生效（看 `CLAUDE_CODE_REMOTE`），以及那台机器上 `CLAUDE_CODE_REMOTE` 的值（钥匙只说有没有，不打印）。
服务器会话大多是开发会话派去干活的，收工钩子挡它们没有意义；查出来没有钩子是正常的，**不用装**。
