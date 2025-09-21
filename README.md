# ai-testing

# 环境安装
- uv init
- uv venv
- source .venv/bin/activate
- uv sync

- 使用 uv sync:如果您有一个 pyproject.toml 文件，并且想要安装其中定义的所有依赖项，可以使用 uv sync 命令，它会读取 pyproject.toml 和 uv.lock 文件并进行安装。
- 使用 uv install:另一种方法是使用 uv install <file> 命令来安装指定文件中的依赖，例如：uv install --no-deps --frozen uv.lock，这会根据 uv.lock 文件安装依赖。


