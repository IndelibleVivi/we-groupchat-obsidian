# we-groupchat-obsidian 离线源码包备用说明

公开 repo 是项目的正式分发与更新入口：

<https://github.com/IndelibleVivi/we-groupchat-obsidian>

这份文件只随 exact-commit sanitized zip 生成，用于没有 Git/网络条件或需要冻结
archival artifact 的历史 offline fallback。完整隐私边界、MCP read-only / sending retirement 规则、Obsidian
工作流和开发说明请看 `README.zh-CN.md`。

## 先确认

- 只支持 macOS。
- 需要 Python 3.10+、已登录的微信桌面版、Xcode Command Line Tools。
- 需要一个 AI provider API Key，或者本地 Ollama。
- 这不是微信/Tencent 官方软件，不是机器人，也不是远程服务。
- 默认数据目录是 `~/.we-groupchat-obsidian/`，API Key 存在 macOS Keychain。
- 云端 AI 会收到你要求总结的聊天文本；想尽量本地化就用 Ollama。

## 第一次运行

1. 解压整个文件夹，不要只拷贝某一个 `.command` 文件。
2. 右键根目录的 `启动.command`，选择“打开”，再在弹窗中确认打开。
3. 脚本需要创建/更新 `.venv` 并安装 dependencies 时会先询问；输入 `y` 才继续，不同意就退出。
4. 菜单栏出现图标后，进入设置，选择 AI provider 并填写 API Key。

macOS 可能在菜单 app 启动后询问一次 WeChat App Data 访问。请确认发起者是本项目 app；
source guard 和资源索引都在这只长驻 app 内运行，不会每 300 秒启动一只新 Python 来重复询问。
历史补链接不读取附件 bytes；附件解析只接受本次 app 会话授权，CLI 则必须在单次 `run` 上显式传 `--resolve-files`。
Mounted backup 完成一轮后，可从菜单选择 `📂 在 Finder 打开文件备份`，或打开 target 下的
`wgo-resource-backup/00-打开微信资源备份.md`；待解析项会与真正可打开的备份文件分开显示。

这是 source-distributed macOS menu-bar app，不包含已签名 installer、`.dmg` 或
bundled Python runtime。

如果微信更新后需要重新授权，普通启动不会偷偷重签名。确认要继续时再运行：

```bash
./启动.command --allow-wechat-resign
```

这一步可能会退出微信，并要求输入 Mac 登录密码；终端输入密码时不显示字符是正常的。

## 常用入口

```bash
./启动.command
./launchers/配置关注推送.command
./launchers/健康检查.command
./launchers/刷新数据源.command
./launchers/历史总结到Obsidian.command
./launchers/整理Obsidian输出.command
./launchers/安装自动启动.command
./launchers/卸载自动启动.command
./launchers/补跑遗漏笔记.command --allow-transient-wechat-source-read
```

补跑（包括只读 audit）会读取受保护的微信数据目录，所以必须显式加
`--allow-transient-wechat-source-read`；macOS 可能为这次单独运行弹一次文件权限窗。
没有这个 flag 时命令会在读取前直接退出。

## 建议先跑一次健康检查

```bash
./launchers/健康检查.command
```

默认输出是 redacted 的，适合排查 DB/key、LaunchAgent、通知 identity 和 Obsidian 输出状态。只有本机私下 debug 才考虑加 `--sensitive`。

## 不要上传或转发这些东西

- `.venv/`
- `.git/`
- `~/.we-groupchat-obsidian/`
- `~/.wechat-summary/`
- `all_keys.json`
- `*.db`
- `*.log`
- API Key、截图里的 token、真实聊天导出、Obsidian 私人 vault 内容

这个 zip 只包含生成时冻结的 `share-manifest.json` allowlist。无 `.git` 的解压目录
再次打包时也只会复制该 manifest 中逐项校验过的文件。能访问 GitHub 时，请改用
公开 repo 的 `git clone` / `git pull --ff-only`，不要继续转发或二次维护 zip。
