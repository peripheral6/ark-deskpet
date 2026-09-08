# Ark Deskpet

基于 [AstrariaX/Ark-codex-skill](https://github.com/AstrariaX/Ark-codex-skill) 的独立Windows桌宠软件，由增强版skill模板派生。不是Codex原生Custom pets。

## 启动

需要Python 3.10+，在本目录执行 `python setup_env.py . --skip-browser`，然后双击 `启动桌宠.bat`。Python 3.14已通过事件与UI测试。

自带遥和予愿安洁莉娜。右键可切换角色、调整倍速/大小和设置；双击隐藏字幕。字幕仅显示状态，中英文切换、居中、透明背景。

## 状态同步

后台每250ms增量读取本机Codex日志：开始后循环移动，完成后互动一次，中断后坐下。设置可关闭联动或指定跟踪任务ID。
留空时选择启动时最近更新的日志并固定跟踪；不会跟随当前打开的任务页面。日志写入有延迟，异常退出未写终止事件时无法可靠判定结束。ChatGPT进程检测不等于读取ChatGPT任务状态。

## 维护与验证

模板功能在 peripheral6/Ark-codex-skill 的 ark-codex-skill/assets/deskpet-app 维护；此仓库是独立运行发行目录。导出和抽帧仍由skill负责。
不要提交个人settings.json、日志、.venv或任务ID。模板同步时保留本仓库默认选择遥的设置。

运行 `python -m unittest discover -s tests -v` 验证，测试环境需安装PySide6。

## 来源

原始程序来自上述仓库，保留作者来源。角色素材来源PRTS，属于各自权利人；用于个人学习和自用。源仓库当前没有独立LICENSE文件，本仓库不擅自替其代码或角色素材授予新许可证。对外发布前需确认适用授权。
