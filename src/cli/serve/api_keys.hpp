#ifndef GUFO_SERVER_API_KEYS_HPP_
#define GUFO_SERVER_API_KEYS_HPP_

#include <array>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace gufo::server {

/// What a key may do. Monitoring keys read health, readiness, models, props,
/// slots and metrics; they cannot submit work. Admin keys read the same and
/// also the content of queued, running and finished requests; they cannot
/// submit work either.
enum class ApiKeyAccess { kInference, kMonitor, kAdmin };

struct ApiKey {
  /// Empty for the single `--api-key`; otherwise the file's NAME.
  std::string name;
  /// Lowercase hex SHA-256 of the bearer credential.
  std::string digest;
  ApiKeyAccess access{ApiKeyAccess::kInference};
  /// Served model names this key may request; empty allows every model.
  std::vector<std::string> models;

  [[nodiscard]] bool allows_model(std::string_view model) const;
};

/// Lowercase hex SHA-256 of a bearer credential, as stored in key files.
[[nodiscard]] std::string ApiKeyDigest(std::string_view credential);

/// The credentials a server accepts. Immutable once built; reloads replace
/// the whole table.
class ApiKeyTable {
public:
  ApiKeyTable() = default;

  /// One unnamed key with full access (`--api-key`).
  static ApiKeyTable FromKey(std::string_view key);

  /// Parses an `--api-key-file`. One key per line:
  ///   NAME sha256:HEX [access=inference|monitor|admin] [models=NAME,...]
  /// Blank lines and lines starting with `#` are ignored. Returns nullopt and
  /// sets `*error` (with the line number) for any invalid line, duplicate
  /// name or duplicate credential, or a file without keys.
  static std::optional<ApiKeyTable> Parse(std::string_view text,
                                          std::string* error);

  /// Reads and parses `path`; errors name the file.
  static std::optional<ApiKeyTable> Load(const std::filesystem::path& path,
                                         std::string* error);

  /// The key whose digest matches the credential, or nullptr.
  [[nodiscard]] const ApiKey* match(std::string_view credential) const;

  [[nodiscard]] bool empty() const noexcept { return keys_.empty(); }
  [[nodiscard]] std::size_t size() const noexcept { return keys_.size(); }

private:
  std::vector<ApiKey> keys_;
};

/// The credentials a running server accepts: either the single `--api-key` or
/// the table of an `--api-key-file`, which `reload_if_changed` re-reads while
/// serving. Thread-safe; `table()` hands out immutable snapshots.
class ApiKeyAuthority {
public:
  /// At most one of `api_key` and `api_key_file` may be set. Throws
  /// std::invalid_argument when both are, or when the file does not load.
  ApiKeyAuthority(std::string_view api_key, std::filesystem::path api_key_file);

  /// Null when authentication is off.
  [[nodiscard]] std::shared_ptr<const ApiKeyTable> table() const;
  [[nodiscard]] bool from_file() const noexcept { return !file_.empty(); }

  /// Replaces the table when the file changed, at most once a second. A file
  /// that no longer parses is logged and the previous keys keep serving, so a
  /// half-written file neither locks everyone out nor opens the server.
  void reload_if_changed();

private:
  std::filesystem::path file_;
  mutable std::mutex mutex_;
  std::shared_ptr<const ApiKeyTable> table_;
  /// Device, inode, size and modification time of the last loaded file.
  std::array<std::int64_t, 4> stamp_{};
  std::chrono::steady_clock::time_point checked_{};
};

}  // namespace gufo::server

#endif  // GUFO_SERVER_API_KEYS_HPP_
