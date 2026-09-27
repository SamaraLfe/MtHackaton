Сборка документации
===================

Зависимости Sphinx перечислены отдельно в requirements-docs.txt, но устанавливаются
в общий Docker-образ вместе с runtime. scripts/start_backend.py собирает HTML
перед запуском backend. При локальном запуске через run_local.py сборка ручная.

macOS / Linux
-------------

.. code-block:: sh

   python -m pip install -r requirements.txt -r requirements-docs.txt
   python -m sphinx -b html docs/sphinx docs/_build/html -W --keep-going

Или из каталога ``docs/sphinx``:

.. code-block:: sh

   make html

Windows
-------

.. code-block:: powershell

   python -m pip install -r requirements.txt -r requirements-docs.txt
   .\docs\sphinx\make.bat html

Результат открывается из ``docs/_build/html/index.html``. Папка сборки не
хранится в Git: пересоберите её после изменений docstrings или конфигурации.

Проверка перед публикацией
--------------------------

Используйте строгую сборку, указанную выше: ``-W`` превращает предупреждения
Sphinx в ошибку, ``--keep-going`` показывает все проблемы за один запуск.
Для внешних ссылок можно отдельно выполнить ``make linkcheck``. Эта проверка
ходит в сеть и поэтому не является обязательной для локальной разработки.

Добавление модуля
-----------------

#. Напишите docstring для модуля и публичных классов/функций.
#. Добавьте ``automodule`` в подходящую страницу либо создайте новую ``.rst``.
#. Включите страницу в ``index.rst``.
#. Выполните строгую HTML-сборку.

Sphinx показывает сигнатуры из исходного кода, а расширение ``viewcode`` даёт
ссылку на строку реализации. Не помещайте в docstrings токены, реальные
данные пользователей или ключи доступа: они попадут в HTML-сборку.
