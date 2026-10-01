# Clock 0.5.0
that is in portuguese plz traduct /:

Clock é uma linguagem orientada a objetos, executada por `Clock`. Nesta versão, `main.py` é o núcleo único: runtime, módulos, histórico e gerenciamento de pacotes.

## Estrutura

```text
Clock-language/
├── main.py            # coração de tudo
├── Clock              # lançador da linguagem
├── ClockInstall       # lançador do gerenciador
├── ClockInst          # alias compatível
├── clock.py           # compatibilidade, chama main.py
├── ClockPacks/        # pacotes instalados
└── packages/          # pacotes-modelo distribuíveis
```

## Clock

```bash
Clock arquivo.clk
Clock -v
Clock -h
Clock -H arquivo.clk
Clock -u arquivo.clk
Clock -a arquivo.clk
```

`-H` mostra cada comando do histórico e, abaixo, suas saídas. `-u` ignora `sleep()`. `-a` instala automaticamente uma dependência ausente, pausando a execução enquanto o download acontece.

## Variáveis e sistema

```clk
set nome as "Clock"
set n as 10
set linha as @
set copia as $linha

get CLASSES.std().print(@)
get sleep(1000)
get die("fim")
```

`@` é a linha atual no instante em que é avaliado. Quando guardado em variável, o valor não muda. `$nome` referencia uma variável.

## Módulos

Math e Str são pré-instalados. Fs é instalável.

```clk
import Math as m
import Str as s
import Fs as fs

set n as $m.sqrt(81)
set upper as $s.upper("clock")
set texto as $fs.read("test.txt")

set arquivo as $fs.requestWrith("saida.txt")
get $arquivo.addLine("linha")
get $arquivo.rmvLine(0)
get $arquivo.mvLine(0, 1)
get $arquivo.finish()
```

## DATE

```clk
set d as \CLASSES.DATE()\
get CLASSES.std().print($d.day)
get CLASSES.std().print($d.year)
get CLASSES.std().print($d.mth)
get CLASSES.std().print($d.s)
get CLASSES.std().print($d.min)
get CLASSES.std().print($d.h)
get CLASSES.std().print($d.ms)
get CLASSES.std().print($d.wd)
```

## EasyTime

`EasyTime` não é pré-instalado. Depois de instalar:

```bash
ClockInstall install EasyTime
```

use:

```clk
import EasyTime as time
set agora as $time.get("dd/mm/yy - minmin:hh")
get CLASSES.std().print($agora)
```

O formato pedido `dd/mm/yy - minmin:hh` produz algo no estilo `01/10/26 - 14:25`. Outros tokens disponíveis: `d`, `dd`, `yy`, `yyyy`, `mth`, `mm`, `hh`, `minmin`, `ss`.

## Histórico

```clk
get CLASSES.HISTORY[0]
get CLASSES.HISTORY.START
get CLASSES.HISTORY.PAUSE
```

`HISTORY[n]` é somente leitura. Usado diretamente com `get`, ele reproduz o comando. Guardado em variável, vira uma string normal.

## Pacotes

Por padrão, `ClockInstall` usa o repositório GitHub `abs-js/CPM`, branch `main`. O banco de dados esperado é `packages.json`; cada entrada aponta para um diretório de pacote, normalmente em `packages/NOME`. O destino local é `ClockPacks/NOME`.

```bash
ClockInstall install Fs
ClockInstall -r install EasyTime
ClockInstall uninstall Fs
ClockInstall list
ClockInstall checkConn
ClockInstall searchPack time
ClockInstall -i EasyTime
ClockInstall -d EasyTime
```

`-r` mostra progresso de download com percentual e bytes. `-i` mostra informações do pacote. `-d` verifica manifesto, versão, arquivos esperados, arquivo principal e extras não previstos no banco.

### Formato do banco `packages.json`

```json
{
  "packages": {
    "EasyTime": {
      "version": "0.1.0",
      "path": "packages/EasyTime",
      "main": "main.clk",
      "files": ["package.json", "main.clk"]
    }
  }
}
```

### Pacotes nativos

Um pacote que precisa de acesso de sistema pode declarar `native` no `package.json`. `Fs`, por exemplo, usa `"native": "Fs"`. A implementação continua centralizada em `main.py`; a instalação só controla a disponibilidade do módulo.

## Limitação do repositório remoto

O código aponta por padrão para `https://github.com/abs-js/CPM`. Na verificação feita para esta versão, a URL pública retornou 404, então a existência/estrutura atual do repositório não pôde ser confirmada. O cliente está preparado para o layout descrito acima e mostrará o erro real se o repositório estiver privado, renomeado ou sem os arquivos esperados.
