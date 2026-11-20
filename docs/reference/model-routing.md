
## Codex Desktop 可见性

模型路由的 `configured`、`resolvable`、`desktop_visible` 和 `request_verified` 必须分别记录。配置文件或模型目录中存在 OpenCode 标识，只能证明前两项中的一项或两项；不能证明 Desktop 选择器已经刷新，也不能证明真实请求已经完成。修改模型目录后先重新打开选择器，必要时完全重启 Codex；GSP worker 仍可通过独立 profile 与显式 `--model` 启动计划工作。
