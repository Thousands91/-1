#!/data/data/com.termux/files/usr/bin/bash
# 阿萨尔X10启动器（坑37 合规姿势：常驻 proot 会话 + sleep infinity 持有）
# remote_ctl 看护会在60秒内自动拉起 NapCat / bot / netmon / offline_watch
# 用法: bash ~/x10_run_asar.sh  然后 Home 切走，别退出这个 Termux 会话
termux-wake-lock 2>/dev/null
proot-distro login ubuntu -- bash -c '
setsid bash /root/remote_ctl.sh >> /root/remote_boot.log 2>&1 < /dev/null &
setsid bash /root/asar_sync.sh loop >> /root/asar_sync_boot.log 2>&1 < /dev/null &
sleep infinity
'
