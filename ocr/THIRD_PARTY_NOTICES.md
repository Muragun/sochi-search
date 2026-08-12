# Данные языков Tesseract

Каталог `tessdata` содержит модели `rus` и `eng`, используемые только для
локального OCR PDF. Они получены из пакетов:

- `@tesseract.js-data/rus` 1.0.0;
- `@tesseract.js-data/eng` 1.0.0.

Источник пакетов: проект `naptha/tessdata`. В метаданных пакетов указана
лицензия MIT. Модели устанавливаются внутрь `/opt/sochi-search/ocr/tessdata` и
не требуют загрузки из сети во время установки или работы.

Автор пакетов: Balearica и участники проекта naptha/tessdata.

## Текст лицензии MIT

Copyright (c) Balearica and naptha/tessdata contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

Контрольные суммы исходных распакованных моделей:

```text
eng.traineddata  5dc5d8d640a212c9d6184921ba103b186f50e0fed9ee716c53e6b312b400d747
rus.traineddata  eb9be824435f6bb0f993925acb85fd842c8418d6db7613c818e749e619a1ad6d
```
