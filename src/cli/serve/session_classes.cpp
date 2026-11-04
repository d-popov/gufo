#include "src/cli/serve/session_classes.hpp"

#include <algorithm>
#include <charconv>
#include <stdexcept>
#include <system_error>
#include <utility>

namespace gufo::server {

bool SessionClassSet::Add(std::string_view spec,
                          std::vector<TextSessionClass>* classes,
                          std::string* error) {
  const auto invalid = [&] {
    *error =
        "--session-class expects NAME=COUNTxCONTEXT with COUNT at least 0 "
        "and CONTEXT at least 1, e.g. main=1x262144; got '" +
        std::string(spec) + "'";
    return false;
  };
  const std::size_t equals = spec.find('=');
  if (equals == std::string_view::npos || equals == 0 ||
      equals + 1 >= spec.size()) {
    return invalid();
  }
  std::string name(spec.substr(0, equals));
  const std::string_view shape = spec.substr(equals + 1);
  const auto separator = shape.find('x');
  if (separator == std::string_view::npos)
    return invalid();
  std::size_t count = 0;
  std::uint32_t context = 0;
  const auto* count_end = shape.data() + separator;
  const auto* context_end = shape.data() + shape.size();
  const auto parsed_count = std::from_chars(shape.data(), count_end, count);
  const auto parsed_context =
      std::from_chars(count_end + 1, context_end, context);
  if (parsed_count.ec != std::errc{} || parsed_count.ptr != count_end ||
      parsed_context.ec != std::errc{} || parsed_context.ptr != context_end ||
      context == 0)
    return invalid();
  if (std::ranges::any_of(*classes,
                          [&](const auto& c) { return c.name == name; })) {
    *error = "--session-class name '" + name + "' is given twice";
    return false;
  }
  // A class without sessions is switched off: nothing is allocated for it and
  // the model name is not served.
  if (count != 0)
    classes->push_back(
        {.name = std::move(name), .count = count, .max_context = context});
  return true;
}

SessionClassSet::Totals SessionClassSet::totals() const {
  Totals totals;
  for (const auto& c : classes_) {
    totals.max_context = std::max(totals.max_context, c.max_context);
    totals.sessions += c.count;
    totals.context_tokens += c.count * c.max_context;
  }
  return totals;
}

std::string SessionClassSet::missing_context() const {
  const auto found =
      std::ranges::find(classes_, 0u, &TextSessionClass::max_context);
  return found == classes_.end() ? std::string{} : found->name;
}

std::size_t SessionClassSet::resolve(std::string_view model) const {
  if (classes_.empty() || model.empty())
    return 0;
  const auto found =
      std::ranges::find(classes_, model, &TextSessionClass::name);
  if (found == classes_.end())
    throw std::invalid_argument("model '" + std::string(model) +
                                "' is not served by this process");
  return static_cast<std::size_t>(found - classes_.begin());
}

std::vector<TextGenerationBackend::ServedModel> SessionClassSet::served_models()
    const {
  std::vector<TextGenerationBackend::ServedModel> models;
  models.reserve(classes_.size());
  for (const auto& c : classes_)
    models.push_back({c.name, c.max_context});
  return models;
}

std::string SessionClassSet::describe() const {
  std::string text;
  for (const auto& c : classes_) {
    text += (text.empty() ? "" : ",") + c.name + ':' + std::to_string(c.count) +
            'x' + std::to_string(c.max_context);
  }
  return text;
}

std::string SessionClassSet::load_line(
    std::size_t model_context,
    const std::function<std::size_t(std::uint32_t)>& session_bytes) const {
  const auto sum = totals();
  std::size_t state_bytes = 0;
  for (const auto& c : classes_)
    state_bytes += c.count * session_bytes(c.max_context);
  return "event=session_classes classes=" + describe() +
         " sessions=" + std::to_string(sum.sessions) +
         " context_tokens=" + std::to_string(sum.context_tokens) +
         " session_state_bytes=" + std::to_string(state_bytes) +
         " model_context=" + std::to_string(model_context);
}

}  // namespace gufo::server
