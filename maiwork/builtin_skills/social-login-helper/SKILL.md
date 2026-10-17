---
name: social-login-helper
description: 教管理员怎么拿社交平台的 Cookie。管理员说「帮我配 Twitter」「小红书登录」等时使用。
license: AGPL-3.0-or-later
metadata:
  maiwork-roles: main
  maiwork-builtin: "true"
  version: "1.0"
---

# 社交平台登录助手

管理员要配社交平台的 Cookie，一步步教：

## Twitter/X

1. 打开浏览器，登录 Twitter/X。
2. 按 F12 打开开发者工具，切到「网络」（Network）标签。
3. 刷新页面，随便点一个请求。
4. 在「请求头」（Request Headers）里找到 `Cookie:` 开头的一行，复制完整值，必须包含 `auth_token` 和 `ct0`。
5. 粘贴到「设置 → 登录态 → Twitter/X」的输入框里。

## 小红书

1. 打开浏览器，登录小红书。
2. 按 F12 打开开发者工具，切到「应用」（Application）标签。
3. 左边选「Cookies」→「https://www.xiaohongshu.com」。
4. 切到「网络」，刷新页面，找到已登录请求的请求头，复制完整 Cookie，建议包含 `a1` 和 `web_session`。
5. 直接粘贴到「设置 → 登录态 → 小红书」，保存后检测，以检测结果为准。

## Reddit

1. 打开浏览器，登录 Reddit。
2. 按 F12 打开开发者工具，切到「应用」（Application）标签。
3. 左边选「Cookies」→「https://www.reddit.com」。
4. 切到「网络」，刷新页面，复制已登录请求头中的完整 Cookie。
5. 粘贴到「设置 → 登录态 → Reddit」，保存后检测，以检测结果为准。

## 注意

- Cookie 会过期，如果搜索突然失败了，让管理员重新拿一次。
- 别把 Cookie 告诉任何人，也别发到群里。

- 凭据只能由管理员直接填写网页输入框；不要让管理员发到群聊、管理员对话、工具参数或日志中。
- 启用平台会自动在 MaiWork 数据目录的独立环境安装依赖。未安装完成时先等待；B站和 YouTube 的公开搜索不需要 Cookie。
- 「已保存」不等于「登录有效」；检测使用只读搜索，失败后按页面提示重新保存 Cookie。不要声称已实际登录或已经搜到，除非检测确实通过。
