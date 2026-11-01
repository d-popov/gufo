#ifndef GUFO_SERVER_SESSION_CLASSES_HPP_
#define GUFO_SERVER_SESSION_CLASSES_HPP_

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include "src/cli/serve/text_generation_backend.hpp"
#include "src/cli/serve/text_model_runner.hpp"

namespace gufo::server {

/// The named groups of sessions behind `--session-class`: how they are
/// parsed, what the model and its pool are sized for, which one a request's
/// model name selects, and how they are listed and logged. An empty set means
/// one model ID serves every session.
class SessionClassSet {
public:
  SessionClassSet() = default;
  explicit SessionClassSet(std::vector<TextSessionClass> classes)
      : classes_(std::move(classes)) {}

  /// Parses one `--session-class NAME=COUNTxCONTEXT` (COUNT sessions of
  /// CONTEXT tokens, both at least 1, served under model NAME) and appends
  /// it. Names are unique across the invocation. False sets `*error`.
  static bool Add(std::string_view spec, std::vector<TextSessionClass>* classes,
                  std::string* error);

  [[nodiscard]] bool empty() const noexcept { return classes_.empty(); }
  [[nodiscard]] const std::vector<TextSessionClass>& classes() const noexcept {
    return classes_;
  }

  /// What the model and its shared scratch are sized for: the largest
  /// context, every session of every class, and the context they hold.
  struct Totals {
    std::uint32_t max_context{0};
    std::size_t sessions{0};
    std::size_t context_tokens{0};
  };
  [[nodiscard]] Totals totals() const;

  /// The first class lacking a context, or empty when all have one.
  [[nodiscard]] std::string missing_context() const;

  /// The class a request's served model selects; every request uses class 0
  /// when the set is empty or the model unnamed. Throws std::invalid_argument
  /// for a name no class serves.
  [[nodiscard]] std::size_t resolve(std::string_view model) const;

  /// One served model per class, in order.
  [[nodiscard]] std::vector<TextGenerationBackend::ServedModel> served_models()
      const;

  /// "NAME:COUNTxCONTEXT,..." for logs.
  [[nodiscard]] std::string describe() const;

  /// The loader's `session_classes` log line: the classes, what they hold and
  /// the device state they claim, from `session_bytes(context)` per session.
  [[nodiscard]] std::string load_line(
      std::size_t model_context,
      const std::function<std::size_t(std::uint32_t)>& session_bytes) const;

private:
  std::vector<TextSessionClass> classes_;
};

}  // namespace gufo::server

#endif  // GUFO_SERVER_SESSION_CLASSES_HPP_
