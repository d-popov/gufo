# Text session classes

Text (`text.hpp`): `TextSessionClass` names a group of identical sessions with
its own context, and `TextRunnerOptions::session_classes` carries them (the
runner is created for the largest class). `TextModelRunner` gains
`CreateSessionClassAdapter`, `PinnedCaptureBytes` and `SessionStateBytes`, which
a component-cache runner overrides to serve a class at its own context; the
defaults refuse classes, charge nothing and reuse the runner's per-request
claim. `ChatRequest::model` names the served model that runs a request, and
`TextPromptContext::Images` reports a request's image work as
`TextImageMetrics`. Source compatible; extensions that implement
`TextModelRunner` or `TextPromptContext` rebuild.
