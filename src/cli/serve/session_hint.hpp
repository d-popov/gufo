#ifndef GUFO_SERVER_SESSION_HINT_HPP_
#define GUFO_SERVER_SESSION_HINT_HPP_

#include <string>
#include <string_view>
#include <utility>

#include "src/core/json.hpp"

namespace gufo::server {

/// A name a client gives the conversation or agent behind a request: the
/// OpenAI `prompt_cache_key` field. Several agents often share one API key;
/// the hint lets the cache keep each one's session apart.
///
/// It only chooses which idle session serves a request, and is always taken
/// under the authenticated client (key name or peer address), so it cannot
/// borrow another client's sessions. Quotas and logs ignore it.
///
/// The HTTP server binds a scope to the connection thread that runs a
/// handler; the handler sets the hint once it has read the body, and the
/// scheduler reads it where the request is submitted, on that same thread.
class SessionHint {
public:
  /// The longest hint used; a longer one is ignored, not truncated, so two
  /// long hints cannot collide.
  static constexpr std::size_t kMaxBytes = 128;

  class Scope {
  public:
    Scope() : previous_(std::exchange(Slot(), std::string())) {}
    ~Scope() { Slot() = std::move(previous_); }
    Scope(const Scope&) = delete;
    Scope& operator=(const Scope&) = delete;
    Scope(Scope&&) = delete;
    Scope& operator=(Scope&&) = delete;

  private:
    std::string previous_;
  };

  /// Records `body["prompt_cache_key"]` when it is a short string.
  static void FromBody(const json::Value& body) {
    if (!body.is_object())
      return;
    const auto* key = body.find("prompt_cache_key");
    if (key == nullptr || !key->is_string())
      return;
    const std::string& text = key->str();
    if (!text.empty() && text.size() <= kMaxBytes)
      Slot() = text;
  }

  /// The hint of the request being handled on this thread; empty if none.
  [[nodiscard]] static std::string Current() { return Slot(); }

private:
  static std::string& Slot() {
    static thread_local std::string hint;
    return hint;
  }
};

}  // namespace gufo::server

#endif  // GUFO_SERVER_SESSION_HINT_HPP_
