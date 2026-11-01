#ifndef GUFO_SERVER_HTTP_AUTHORIZATION_HPP_
#define GUFO_SERVER_HTTP_AUTHORIZATION_HPP_

#include <functional>
#include <optional>
#include <string>

#include "src/cli/serve/api_keys.hpp"
#include "src/cli/serve/http_server.hpp"

namespace gufo::server {

/// Authenticates `request` against `keys` and applies the matching key's
/// access and model limits, recording the key (and, for a named key, its name
/// as the client) on the request. Returns the refusal, or nullopt to proceed.
/// `route_model` names the served model the request's route dispatches to, or
/// "" for none; a model a client puts in its body or query cannot steer it.
std::optional<HttpResponse> AuthorizeRequest(
    const ApiKeyAuthority& keys, HttpRequest& request,
    const std::function<std::string(const HttpRequest&)>& route_model);

/// Appends `key=NAME` to request log details when a named key authorized it.
std::string WithKeyField(std::string details, const HttpRequest& request);

}  // namespace gufo::server

#endif  // GUFO_SERVER_HTTP_AUTHORIZATION_HPP_
