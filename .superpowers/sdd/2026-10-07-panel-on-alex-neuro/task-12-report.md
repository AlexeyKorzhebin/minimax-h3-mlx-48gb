# Task 12 — отчёт

## Сделано
- app.js: gpuBanner, notificationEvents, sceneTagIssues, projectTagWarningsHtml, projectSettingsHtml, tagSuggestions, projectRouteHtml, projectReferencesHtml, libraryCardsHtml + новая чистая referencesPayload.
- DOM: опрос /api/gpu в poll(), плашка, «Освободить карту» (подтверждение по release_needs_confirm), «Выгрузить/Вернуть Qwen», уведомления (первый снимок молчит), форма и список референсов, закрепление референсов в модалке проекта, чекбокс апскейла, i2v_prefix, «Черновая сборка», подсказка тегов, поле тегов чата (tags в теле сообщения), скрытие «Показать в Finder» вне darwin.
- web.py: _gpu_state отдаёт idle_release_at (только при пустой очереди и своём движке).
- index.html/style.css: минимальные блоки; цвета через токены (вместо литералов из брифа — их ловит test_no_color_literal_lives_outside_the_tokens_block).

## Отличия от брифа
1. `projects` в notifyFromState — не переменная, а DOM-id (window.projects): TypeError в браузере, найден скриншотом/консолью; заменено на state.projects.
2. qwenAction принимает bool и вызывает литеральные пути: регэксп test_the_page_asks_for_its_own_routes... принимал "POST" за URL при `api("POST", route, ...)`.
3. ref-pin: бриф слал ссылки без version — снятие соседней галочки молча обновило бы закреплённую v2 до последней. Добавлена referencesPayload (закреплённые сохраняют версию, новые — без version) + тест.
4. Тест projectReferencesHtml усилен: мутация (д) `<=` выживала (в брифовом тесте не было актуальной закреплённой карточки); добавлена закреплённая @beach v1 и случай без закрепления.
5. Баннер: max-width 34ch, мелкий шрифт, title с полным текстом — без этого перекрывал показания шапки.

## TDD
RED: tests/test_webui_panel.py до реализации — 15 failed (TypeError: app.gpuBanner is not a function; KeyError idle_release_at; нет CSS/маршрутов). referencesPayload: «node refused the module ... app.referencesPayload» до реализации.
GREEN: tests/test_webui_panel.py 16 passed. Полный прогон: 1797 passed, 150 skipped, 0 failed (базис 1781).

## Мутации (все красные)
- (а) без .replace(/^ждём GPU: /) — banner_while_waiting: assert {...} == {...}
- (б) EVERY 30 мин — wait_over_ten: assert [{'kind': 'wait'...}] == []
- (в) next[key] вместо fresh — fire_once: assert [{'kind': 'failed'...}] == []
- (г) без lookbehind TAG_IN_TEXT — scene_tag_issues: assert [{'tag': '@Bo...invalid'}] == [...]
- (д) `<=` вместо `<` — ПЕРВОНАЧАЛЬНО ВЫЖИЛА; после усиления теста: assert '<div class=".../label></div>' == ...
- (е) `prev === null && false` — first_snapshot: TypeError: Cannot read properties of null (reading 'failedIds')
- (ж) без queueEmpty — qwen_restore: assert True is False
- (з) без checked в route — route_html: assert '<label class...' == ...
- (и) без needsTag && seen.size === 0 — tag_warnings: assert '<ul class="t...bob</li></ul>' == ...
- (к) referencesPayload без сохранения версии — references_payload: assert [{'tag': '@al...'}] == ...

## Скриншоты (shots/ рядом с отчётом)
- task12-sglang.png: sglang + мёртвый диспетчер: плашка «Диспетчер GPU не отвечает: GET /status: HTTP 502» (красный фон), «Освободить карту», «Уведомления». Показания работник/очередь/LLM в шапке сжаты — теснота старой вёрстки, редизайн в волне 2.
- task12-library.png: секция «Референсы»: карточка @alice с превью из загруженного png (/media/... отдаёт 200), форма. Добавлена через реальную форму.
- task12-mlx.png: движок mlx — плашки и GPU-кнопок нет (hidden у всех пяти).

## Не проверено в браузере
Модалка проекта (закрепление референсов, апскейл, i2v_prefix, черновая сборка, подсветка тегов) и подтверждение release — нужен проект и работающий диспетчер; покрыты только чистыми функциями и проверкой источников. Реальные Notification не проверялись.
Сервер на :8799 остановлен. Логи: ~/Research/TestVideo/_логи/panel-ui-8799*.log.

# Fix round 1 (ревью opus)

Полный прогон: 1812 passed, 150 skipped, 0 failed (до раунда 1797). Харнес: tests/_panel_ui_check.mjs (реальный app.js, скриптованный fetch, без сервера), вызывается из tests/test_webui_panel.py; JS-сравнения только точные.

1. [Critical] gpuBanner при `dispatcher.gpu === null` в ветке own: память не показывается, называется `dispatcher.gpu_error` (поле лежит на теле диспетчера, не на /api/gpu): «Карту держит панель: H3 (память GPU неизвестна: nvidia-smi недоступен) — освободится кнопкой». pollGpu: renderGpu и notifyFromState каждый в своём try/catch с console.error.
   Тесты: test_banner_when_the_panel_holds_the_card_and_nvidia_smi_is_down, test_a_render_exception_does_not_stop_notifications_or_the_poll.
   Мутации (красные): `const memory = true ?` -> «node refused the module» (TypeError на d.gpu.memory_used_mb); `renderGpu();` без try -> «render_throws: ...app.js:88 ... TypeError».
2. Три действия: (а) projectUpscaleHtml: «Апскейл LTX: <статус>» + «Повторить апскейл» при failed -> POST /api/projects/p1/upscale/retry {} (через withProject); (б) runCancelHtml в run-foot бегущей задачи, DELETE /api/jobs/<id>, alert(answer.message); (в) libraryCardsHtml: поле описания + «Сохранить описание» -> PUT /api/library/alice {description} (libraryUpdateRequest). Всё только на sglang (кроме библиотеки).
   Тесты: upscale_status_and_retry_button, upscale_retry_posts..., cancelling_the_running_job_shows_the_servers_message, saving_a_library_description..., cancel_button_and_library_edit_controls_and_request.
   Мутации: retry route `/upscale/again` -> assert {'status': Tr.../again'} == {...retry'}; убрать alert -> assert {'deletes': ..., 'alerts': []} == {...}; имя карточки с «@» -> assert {'name': '@al...'} == {'name': 'ali...'}; retry-кнопка при любом статусе -> assert '<div class="...button></div>' == '<div class="...X: идёт</div>'.
3. Обработчики change(route, ref-pin)/focusout(i2v)/click(draft) переведены на withProject(() => api(...)): без alert, без openProjectModal (выбор провайдера сценария не сбрасывается), с projectBusy; галочка возвращается, потому что withProject перерисовывает панель из состояния сервера. Тест route_error (сервер: 409, upscale выключен): puts [[route,{upscale:true}]], alerts [], projectRereads 1, providerReloads 0, boxChecked False.
   Мутация (api().then(openProjectModal).catch(alert)): assert {'puts': [...' ... 'providerReloads': ...} != ожидаемого.
   Замечание (старое поведение, не трогал): после ошибки withProject её плашка тут же стирается успешным refreshProjectDetail (clearProjectError) — для всех действий проектов.
4. Плашка и кнопки GPU/Qwen вынесены из .rail-clock в отдельную строку `.gpu-row` под шапкой (index.html/style.css); строка скрыта, пока все дети hidden (mlx: display none, проверено computed style). Скриншоты (смотрел): shots/task12r1-sglang.png — плашка «Диспетчер GPU не отвечает: GET /status: HTTP 502», «Освободить карту», «Уведомления» под шапкой, показания шапки не перекрыты; shots/task12r1-mlx.png — шапка без новой строки, идентична прежней.
5. Теги чата: поле заполняется из session.tags при открытии диалога (chat.tagsKnown), ход несёт `tags` только если поле отличается от известного (chatTagsBody); очистка поля = tags []; невалидный тег (@Bob, @a_b, alice) — сообщение в #chat-tags-error, ход не отправляется, текст не теряется. Выбор «заполнять из сессии + слать по отличию»: сервер хранит session.tags и GET /api/chat/<id> их отдаёт, значит поле остаётся честным отражением сессии.
   Тест test_chat_tags_are_sent_only_when_changed_and_bad_tags_are_reported. Мутация (убрать строку «поле не тронуто»): assert {'body': {'ta...'error': None} == {'body': {}, 'error': None}.
6. .mjs-харнес: release (confirm -> второй POST {confirm:true}; отказ -> второго нет; сбой второго -> alert и повторный опрос), notify (первый снимок молчит; assembly done -> «Проект готов»; перенос lastWaitNotifyMs: 11-минутное ожидание не повторяется). Мутации: убрать confirm-гейт, `{confirm:true}`->`{}`, убрать alert внешнего catch (все -> assert {'bodies': ...} == {...}); `lastWaitNotifyMs: null` -> assert {'afterFirst'...body': 'p2'}]} == ...(лишнее уведомление об ожидании); `"dne"` вместо "done" -> assert {...'afterSecond': []} == {...'Проект готов'...}.
7. mlx: projectTagWarningsHtml/projectRouteHtml/projectUpscaleHtml/runCancelHtml принимают engine и на не-sglang возвращают ""; обработчик input на .scenario-prompt молчит на mlx. Тесты test_mlx_shows_no_tag_demands_and_no_upscale_checkbox, test_scene_prompt_input_demands_a_tag_on_sglang_and_stays_silent_on_mlx. Мутации: убрать engine-проверку в warnings -> assert '<ul class="t...ене</li></ul>' == ''; в route -> assert '<label class... сцен</label>' == ''; в runCancelHtml -> assert ' <button typ...нить</button>' == ''; в input-обработчике -> assert {'title': 'ну...sues', True]]} == {'title': '', 'toggles': []}.
8. Второй POST release внутри try: ошибка -> alert + poll (тест release_second_fails_..., мутация п.6).
Часовой пояс не трогал. Литерал-регэксп test_the_page_asks_for_its_own_routes: libraryUpdateRequest отдаёт `name`, путь собирается литералом в api(...).
