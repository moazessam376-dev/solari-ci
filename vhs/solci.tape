Output docs/solci.gif
Set Shell "bash"
Set FontSize 16
Set Width 1280
Set Height 760
Set Padding 24
Set Theme "Catppuccin Mocha"

Type "export PATH=$PWD/.venv/bin:$PATH && clear"
Enter
Sleep 1s

Type "solci inspect moazessam376-dev/crosstalk --job test --runs 20"
Enter
Sleep 6s

Type "solci run moazessam376-dev/Gym-App --job typecheck --cpu 2"
Enter
Sleep 120s
