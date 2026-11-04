#include "src/cli/serve/http_authorization.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <string_view>
#include <utility>

#include "src/core/json.hpp"

namespace gufo::server {
namespace {

std::string ToLower(std::string_view s) {
  std::string out(s);
  for (auto& c : out) {
    c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  }
  return out;
}

HttpResponse Refuse(int status, const char* reason, const char* message,
                    const char* type, const char* code) {
  json::Value error = json::Value::object();
  error["message"] = message;
  error["type"] = type;
  error["code"] = code;
  json::Value body = json::Value::object();
  body["error"] = std::move(error);
  return {.status = status, .reason = reason, .body = body.dump()};
}

/// The credential of a single `Authorization: Bearer` header, or nullopt for
/// none, several, or another scheme.
std::optional<std::string_view> BearerCredential(const HttpRequest& request) {
  std::string_view credential;
  bool found = false;
  for (const auto& [name, value] : request.headers) {
    if (ToLower(name) == "authorization") {
      if (found) {
        return std::nullopt;
      }
      found = true;
      credential = value;
    }
  }
  const auto space = credential.find(' ');
  if (space == std::string_view::npos ||
      ToLower(credential.substr(0, space)) != "bearer") {
    return std::nullopt;
  }
  credential.remove_prefix(space + 1);
  while (credential.starts_with(' ')) {
    credential.remove_prefix(1);
  }
  return credential;
}

/// Routes a monitoring key may read: health, readiness, models, props, slots
/// and metrics. None of them submits work or returns generated content.
bool IsMonitoringRoute(const HttpRequest& request) {
  if (request.method != "GET")
    return false;
  static constexpr std::array<std::string_view, 13> kPaths = {
      "/health",   "/v1/health", "/healthz",   "/ready", "/v1/ready",
      "/readyz",   "/models",    "/v1/models", "/props", "/slots",
      "/v1/slots", "/metrics",   "/v1/metrics"};
  return std::ranges::find(kPaths, request.path) != kPaths.end();
}

/// The route that returns request content. Only an admin key may read it;
/// the handler checks that too, so a server without keys never serves it.
bool IsAdminRoute(const HttpRequest& request) {
  return request.method == "GET" && request.path == "/v1/admin/sessions";
}

}  // namespace

std::optional<HttpResponse> AuthorizeRequest(
    const ApiKeyAuthority& keys, HttpRequest& request,
    const std::function<std::string(const HttpRequest&)>& route_model) {
  auto table = keys.table();
  if (table == nullptr)
    return std::nullopt;
  const auto credential = BearerCredential(request);
  const ApiKey* key = credential ? table->match(*credential) : nullptr;
  if (key == nullptr) {
    auto response = Refuse(401, "Unauthorized", "missing or invalid API key",
                           "authentication_error", "invalid_api_key");
    response.headers.emplace_back("WWW-Authenticate", "Bearer");
    return response;
  }
  // The request keeps the table alive while it holds the key.
  request.api_key = std::shared_ptr<const ApiKey>(std::move(table), key);
  if (!key->name.empty())
    request.client_id = key->name;
  if (key->access == ApiKeyAccess::kMonitor && !IsMonitoringRoute(request)) {
    return Refuse(403, "Forbidden",
                  "this API key may only read health, models, props, slots "
                  "and metrics",
                  "permission_error", "permission_denied");
  }
  if (key->access == ApiKeyAccess::kAdmin && !IsMonitoringRoute(request) &&
      !IsAdminRoute(request)) {
    return Refuse(403, "Forbidden",
                  "this API key may only read health, models, props, slots, "
                  "metrics and admin sessions",
                  "permission_error", "permission_denied");
  }
  if (!key->models.empty() && !IsMonitoringRoute(request)) {
    // Every other route reaches a model, WebSocket upgrades included. Each
    // service serves one model, so check the model of the service the route
    // dispatches to.
    const std::string model = route_model(request);
    if (!model.empty() && !key->allows_model(model)) {
      const std::string message =
          "this API key may not use model '" + model + "'";
      return Refuse(403, "Forbidden", message.c_str(), "permission_error",
                    "model_not_allowed");
    }
  }
  return std::nullopt;
}

std::string WithKeyField(std::string details, const HttpRequest& request) {
  if (request.api_key == nullptr || request.api_key->name.empty())
    return details;
  if (!details.empty())
    details += ' ';
  return details + "key=" + request.api_key->name;
}

}  // namespace gufo::server
