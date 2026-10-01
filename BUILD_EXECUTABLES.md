# Executáveis da Clock

`Clock` e `ClockInstall` nesta distribuição são binários ELF x86-64.

Eles usam o Python 3 instalado no sistema para executar `main.py`, que continua sendo o coração da linguagem.

Isso permite instalar/usar os comandos como executáveis normais, sem precisar chamar `python3 main.py` manualmente.

Arquivos:
- `Clock` — executável da linguagem.
- `ClockInstall` — gerenciador de pacotes.
- `ClockInst` — alias binário de `ClockInstall`.
- `main.py` — implementação central/runtime.
- `clock_launcher.c` — código do pequeno lançador ELF.
