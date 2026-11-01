# 阿萨尔机器人 · 运维手册与踩坑全记录

> 项目：QQ 群机器人「阿萨尔」（苏丹的游戏书店老板人设）
> 架构：NoneBot v2 + OneBot v11 + NapCat 4.18.42，模型 glm-4-flash
> 部署：手机 Termux(Ubuntu proot) 24h 挂机，机器人 QQ 3427943187
> 同步：GitHub 仓库 `Thousands91/-1`，遥控通道 `remote/cmd.txt` / `remote/out.txt`（纯 API）
> 更新：2026-11-01

**用法：每次开工前先通读一遍「铁律」和「坑清单」，动手前对号入座。**

---

## 一、铁律（违反必翻车）

1. **cmd.txt 第一行必须是任务编号（纯数字）**，第二行起才是命令。缺编号 = 循环当异常丢弃，还伪装成"API响应异常"，极易误诊为网络问题。（2026-11-01 实锤，浪费半小时）
2. **每推必验证**：git push 会假成功（exit 0 但远程没有），含 `ghp_` 字面量的文件会被密钥扫描拦截。统一用 GitHub API PUT，推完必须 GET 回读确认。
3. **API PUT 前必先 GET sha**：直接 PUT 已存在文件会 422。
4. **在线状态绝不重启 NapCat**：logincheck 显示 [在线] 时碰都不要碰登录相关进程。
5. **保险丝**：24h 内自动拉起 NapCat ≥3 次即熔断（只报警不重启），防"崩→扫→崩"恶性循环。人工判断后的重启不受保险丝限制，但必须先看 logincheck。
6. **扫码纪律**：随用随取码，不空转出码；给用户码之前必须确认文件是新生成的（对时间戳），绝不拿旧码充数。
7. **不碰登录站稳后的 NapCat**；NapCat 挂掉多为服务端作废会话，不是进程崩，凭据恢复型重启风控权重低。
8. **不向用户要 QQ 密码**（风控反噬+泄露面大，收益小）。
9. **手机端操作最小化**：一次最多一条可粘贴命令；能遥控做的绝不麻烦用户。
10. **先验证再让用户行动**：让用户扫码/执行前，自己这边先确认就绪。"确认才是病根"——指的是别反复要用户确认，而不是不验证。

## 二、坑清单（按主题）

### 遥控通道
- **cmd.txt 格式**：`第一行任务号\n命令...`。主循环 `case "$RAW" in [0-9]*)` 判定，非数字开头直接丢弃。
- **out.txt 格式**：`<<<task_N.txt>>>` 打包 + 待发箱 `/root/.remote_pending` 兜底，回传失败留待下轮。
- **git 通道在手机上不可靠**："Failure when receiving data from the peer" 反复出现，API 从未失败 → 终版方案全 API（GET/PUT contents），git 弃用。
- **手机取 token**：不要用 `git credential fill`（无凭据存储会弹交互卡死循环），用 `grep -oE 'ghp_...' /root/.git-credentials`。
- **回传细节**（v4.1 教训）：URL 拼接别拼成 `.../cmd.txt/out.txt`；待发箱文件必须带 `.txt` 后缀否则回传过滤掉。

### 进程管理
- **pgrep -f 自匹配陷阱**：`pgrep -f remote_ctl.sh` 会匹配到 cmdline 里含该字符串的其他命令（包括 proot 包装命令和自己），误判"已在运行"。判定用锁文件 + `kill -0`。
- **单实例锁的坑**：重启后旧 PID 可能被系统其他进程占用 → `kill -0` 误判"已运行" → 新循环静默退出。处理：启动前 `rm -f /root/.remote_pid`（人工确认无真实例时）。
- **多实例清理**：反复启动会并存多实例（重复执行任务/重复拉 NapCat/保险丝计数虚高）。清理：`for p in $(pgrep -f "bash /root/remote_ctl.sh"); do [ "$p" != "保留PID" ] && kill -9 $p; done`。
- **proot 一条式启动会被清场**：`proot-distro login ubuntu -- bash -c 'nohup ... &'` 命令跑完 proot 以 `--kill-on-exit` 收尾，后台循环陪葬。**正确姿势**：交互式 `proot-distro login ubuntu` 进去后 nohup 启动，然后**不要 exit**，直接 Home 切走。
- **竞态**：先读 `/proc/<pid>/cmdline` 再 kill，中间进程可能已消失；升级瞬间新旧循环竞态会回传空文件（验证升级要看下一任务输出，不信当次）。
- **shell 假成功**：`a && b ; echo 完成` 中 echo 必执行，别用尾缀 echo 当成功凭证，用实际状态判定。

### 手机后台（华为）
- **省电模式全局掐后台网络**，优先级高于单 App 白名单 → 已关，永久保持关。
- **被冻结的进程不会自动解冻**，必须重启进程（症状：进程活着、心跳为零、手动跑 API 却通）。
- 长连接（NapCat 挂机）不受影响，被掐的是周期性主动联网（遥控轮询）。
- 电池优化/自启动/wake-lock 部署时已配好，别重复建议；手机重启后 Termux **不会自启**，需手动开 App + 交互 shell 里启动循环。

### NapCat / 配置
- **onebot11 配置 `websocketClients` 必须数组格式**：对象格式被 schema 静默丢弃（不报错！），症状=登录成功但 WS 不接、大脑收不到消息。用 WebUI API 查运行时配置实锤。
- WebUI 认证：POST `/api/auth/login` `{"hash": sha256(token+'.napcat')}`，Bearer 调 `/api/QQLogin/CheckLoginStatus`、`/api/OB11Config/GetConfig`。
- 登录三态权威判定：`bash /root/logincheck.sh` → [在线]/[掉线]/[未登录] + loginPhase + loginError。
- 二维码位置：`/root/Napcat/opt/QQ/resources/app/app_launcher/napcat/cache/qrcode.png`。

### 沙箱（验证场）
- 约 10 分钟空闲清后台进程；休眠杀掉 NapCat 后**快速登录不可靠**（凭据可能未完整落盘），大概率要重新扫码。
- 沙箱当验证场用：账号能不能收发、配置对不对，先在沙箱跑通再上手机。

## 三、标准流程

### 手机重启后恢复
1. 用户打开 Termux（必须手动，无自启）
2. `proot-distro login ubuntu`（交互式进入）
3. `rm -f /root/.remote_pid; nohup bash /root/remote_ctl.sh >/dev/null 2>&1 & sleep 1; echo PID: $(cat /root/.remote_pid)`
4. **不 exit**，Home 切走
5. 遥控下发「状态检查」任务验证回传

### 扫码登录（用户说"出码"才启动）
1. 遥控下发保鲜命令（相册 `Pictures/阿萨尔登录码.png` 每 20 秒更新）
2. NapCat 若未出码则人工重启出码（先看 logincheck + 保险丝状态）
3. QQ 扫一扫 → 相册选码 → 确认登录
4. 遥控跑 logincheck 验证 [在线] → 群里 @阿萨尔 测试
5. 登录成功后不再碰 NapCat

### 下发命令的标准动作
1. PUT cmd.txt（先 GET sha；第一行任务号=上次 LAST+1）
2. 等 60~90 秒 GET out.txt（加时间戳参数防缓存）
3. 没回传先看循环日志（下发任务查 `tail /root/remote_ctl.log`），别急着怀疑网络

## 四、用户协作准则

- 别抓一个新线索就当成新原因——先拿证据。
- 命令要短，一次一条，可整体粘贴。
- 确认才是病根：别反复让用户确认同样的事。
- 别拿用户当试验场：实验先在沙箱做，结论验证了再给用户。
- 用户说不动就是不动（如"先别扫码"），严格照办。
