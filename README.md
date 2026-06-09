# YSM → Figura 模型转换

将 YSM（Yes Steve Model）非加密模型转换为完整的 Figura 头像工程目录。
至于加密？自己想办法。我可不想挨小团体的骂，那帮人看着就吓人
注：这是真的编译出lua文件，而不是拿lua做解释器。

## 使用方法

```bash
# 使用模块方式运行
python -m converter.main <ysm模型文件夹> [输出目录]

# 或者直接运行脚本
python converter/main.py <ysm模型文件夹> [输出目录]

# 示例
python -m converter.main ./MyYsmModel ./output
```

## 输出结构

```
output/模型名/
├── avatar.json              #模型元数据
├── main.lua                 #YSM完整运行时模拟
├── 模型名.bbmodel
├── 模型名_子实体.bbmodel   #没人用的子实体
└── textures/                #纹理文件
```

## 将输出放入 Figura

直接把整个 `output/模型名/` 文件夹复制到 `~/.figura/avatars/` 或
`%appdata%/.figura/avatars/` 即可。
一般需要改改，运气好可以直接用。
虽然有openYSM打底，让我的工作变得更加轻松，但是没轻松到哪里去。希望有极端情况的适配并pr，我也不是很想收集ysm模型