#include "src/cli/serve/session_classes.hpp"

#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

using gufo::server::SessionClassSet;
using gufo::server::TextSessionClass;

void Expect(bool condition, std::string_view message) {
  if (!condition) {
    std::cerr << "Assertion failed: " << message << '\n';
    std::exit(1);
  }
}

bool Parses(std::string_view spec, std::vector<TextSessionClass>* classes) {
  std::string error;
  return SessionClassSet::Add(spec, classes, &error);
}

void TestParsing() {
  std::vector<TextSessionClass> classes;
  Expect(Parses("main=1x163840", &classes), "a class parses");
  Expect(Parses("sub=7x65536", &classes), "a second class parses");
  Expect(classes.size() == 2 && classes[1].name == "sub" &&
             classes[1].count == 7 && classes[1].max_context == 65536,
         "the parsed fields are kept");
  const auto before = classes;
  for (const char* bad :
       {"", "main", "=1x2", "main=", "main=1", "main=x5", "main=0x5",
        "main=5x0", "main=1x2x3", "main=-1x5", "main=1xfive", "sub=1x1"}) {
    Expect(!Parses(bad, &classes), std::string("rejected: ") + bad);
  }
  Expect(classes == before, "a refused class changes nothing");
  std::string error;
  Expect(!SessionClassSet::Add("main=2x4096", &classes, &error) &&
             error.find("twice") != std::string::npos,
         "a repeated name is refused and named");
}

void TestTotalsAndRouting() {
  const SessionClassSet empty;
  Expect(empty.empty() && empty.resolve("anything") == 0,
         "without classes every request uses class 0");
  Expect(empty.totals().sessions == 0, "an empty set holds nothing");

  const SessionClassSet set(
      {{.name = "main", .count = 1, .max_context = 163840},
       {.name = "sub", .count = 7, .max_context = 65536}});
  const auto totals = set.totals();
  Expect(totals.max_context == 163840, "the model is sized for the largest");
  Expect(totals.sessions == 8, "every session of every class counts");
  Expect(totals.context_tokens == 163840 + 7 * 65536, "context sums per class");
  Expect(set.resolve("main") == 0 && set.resolve("sub") == 1,
         "a model name selects its class");
  Expect(set.resolve("") == 0, "an unnamed request uses the first class");
  bool threw = false;
  try {
    (void)set.resolve("other");
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  Expect(threw, "a name no class serves is refused");
  const auto models = set.served_models();
  Expect(models.size() == 2 && models[1].id == "sub" &&
             models[1].context_length == 65536,
         "each class is a served model with its own context");
  Expect(set.describe() == "main:1x163840,sub:7x65536", "the log spelling");
  Expect(set.missing_context().empty(), "all classes have a context");
  Expect(SessionClassSet({{.name = "x", .count = 1, .max_context = 0}})
                 .missing_context() == "x",
         "a class without a context is named");
  const auto line = set.load_line(
      163840, [](std::uint32_t context) { return std::size_t{context} * 10; });
  Expect(line.find("session_state_bytes=" +
                   std::to_string(163840 * 10 + 7 * 65536 * 10)) !=
             std::string::npos,
         "the claim is the sum over sessions of each class's bytes");
}

}  // namespace

int main() {
  TestParsing();
  TestTotalsAndRouting();
  std::cout << "session_classes_test: ok\n";
  return 0;
}
