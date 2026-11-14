# this is the startup setup procedure to follow everytime we start a new project
1. **Install uv:** uv can be installed alredy when you start a new project in pycharm. If this is not the case, run in the command line `pip install uv`
2. **Initialize uv:** If not done when crated the project in pycharm, run `uv init`
3. **Install pre-commit:** run `pip install pre-commit`
4. **Install pytest:** run `pip install pytest`
5. **Install ruff:** run `pip install ruff`
6. **Init Claude:** run `claude` and then invoke `/init`
7. **Init Openspec:** in the terminal run `openspec init`
8. **Init the mattpocock skills:** in Claude invoke `/mattpocock-skills:setup-matt-pocock-skills`
9. **Create a .gitignore fie:** Create the file manually and exclude from the start: 
   - All files in the **.idea** folder
   - All secrets, **.env** files
   - All Claude specific files: **.claude/settings.local.json**
   - All python bytecode caches: **__pycache__/** and ** *.pyc **
10. During the software development, follow the workflow in `~/.claude/skills/sw_development_methodology.md`. It is global
   (loaded through `~/.claude/CLAUDE.md`): don't copy it into the project and don't add a "Mandatory workflow" section to the project's CLAUDE.md.
