# Шаблон спецификации

`specification_v1.xlsx` — неизменённая локальная копия из
`Amadu-A/spec-extracting-ai`, commit `9844c4ee68fbd35840b0e5bb634ab6a5eef0879e`,
путь `app/resources/specification_v1.xlsx`.

SHA-256: `008dd7b7130668acc55e1bfdf96cf4be3b8bc05c1e0c27df0668189a6f14f2c4`.

За основу взят `app/infrastructure/excel.py` того же коммита: добавление источника
в P, перенос ширины/стилей, заполнение восьми строковых полей, RUB и расширение
строк по последней строке шаблона. В Mapper этот единственный exporter дополнен
шестью столбцами T:Y и читает сохранённый approval snapshot.
Во время экспорта обращений к Extracting нет.
