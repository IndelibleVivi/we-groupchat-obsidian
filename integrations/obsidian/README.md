# WGO Reader Observer

这是供桌面 Obsidian 使用的本地可见打开观察器与局部阅读样式，仓库提供 source candidate。安装、启用与实际桌面验收分别进行。它不新增菜单或按钮，不读取 WeChat，不发网络请求，不发系统通知。

普通阅读、检索、关注、静音、等待条件和管理页的「知道了」不依赖此插件。需要自动记录「实际打开了哪个内容版本」，或在阅读模式收起机器属性与重复的文件名标题时，可以安装启用。

1. 在所用 vault 的 `.obsidian/plugins/wgo-reader-observer/` 目录放入本目录的 `manifest.json`、`main.js` 与 `styles.css`。
2. 重启或重新加载 Obsidian，按其社区插件设置启用 **WGO Reader Observer**。WGO 不会自动安装或启用插件。
3. 该 vault 中的受管阅读根目录必须是 `WGO/`。运行使用同一 generation 和 projection root 的 WGO app。
4. 在可见、聚焦的 Markdown pane 中打开 WGO Item 或简报。正常情况下，WGO 的下一次 reader poll 会消费事件；`WGO/阅读状态.md` 显示无法确认事件的有界状态。

插件至少需要 Obsidian 1.7.2。它通过 `getActiveViewOfType(MarkdownView)`、active-leaf/file-open/layout、可见性和窗口焦点事件检查当前 pane，并排除 DeferredView。它还要求页面可见文本与当前磁盘文件逐字一致；未保存的编辑不会被当成已确认的 canonical 版本。API 依据见 [Obsidian API](https://github.com/obsidianmd/obsidian-api/blob/master/obsidian.d.ts) 与 [Deferred views 指南](https://github.com/obsidianmd/obsidian-developer-docs/blob/main/en/Plugins/Guides/Defer%20views.md)。

阅读样式只作用于 `WGO/` 内带完整 WGO 身份属性的 Markdown pane。阅读模式保留正文中的可读标题，收起 Obsidian 重复显示的文件名标题与 `wgo_*` 机器属性；用户自己的关注、静音等属性继续显示。纯机器属性区不占正文空间。切换到编辑模式仍能查看和编辑原始属性，切换到普通笔记或卸载插件会移除该局部样式。样式不会改写 Markdown、重命名文件、修改阅读状态，也不改变可见打开事件的验证条件。

事件只写入 `WGO/.wgo/reader-events/<opaque-id>.json`，先写私有临时文件，再原子改名。事件包括逻辑 Item/简报 ID、generation、显示的 content revision、WGO 内相对路径、文件 SHA-256 和观察时间，不含正文。目录为 `0700`、事件为 `0600`；symlink、越界路径和非普通文件会拒绝。当前文件权限实现只支持具有 POSIX 私有权限语义的桌面环境，尚无 Windows DACL 验收，不应据此宣称 Windows 插件支持。

Python 消费端会再次验证完整 schema、generation、路径、文件身份、历史版本和可信投影，写入 durable receipt 后才移到 `processed/`。无效原件保留；receipt 保证崩溃恢复或重复扫描不会重复记读。目录应留在本机私有 vault 中，不应单独同步或公开分享。

这是可信本地插件与 WGO 之间的协议，不提供对同一操作系统用户伪造文件的密码学认证。系统接受通知、点击通知、文件 mtime 或 WGO 发出的 open 命令都不会记为读过；观察器记录的读过也不会执行「知道了」。完整责任与验证合同见 [semantic reading](../../docs/semantic-reading.md)。
