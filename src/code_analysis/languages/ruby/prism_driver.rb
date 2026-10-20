# frozen_string_literal: true

require "json"
require "prism"
require_relative "call_occurrences"

source = STDIN.read
max_occurrences = if ARGV.empty?
  CallOccurrences::DEFAULT_MAX
elsif ARGV.length > 1
  warn "prism_driver.rb accepts at most one max_occurrences argument"
  exit 1
else
  token = ARGV[0]
  unless token.match?(/\A[1-9]\d*\z/)
    warn "max_occurrences must be a positive integer"
    exit 1
  end
  token.to_i
end
result = Prism.parse(source)
symbols = []
imports = []
calls = []
hashes = []
exceptions = []
effects = []

def node_name(node)
  return node.name.to_s if node.respond_to?(:name) && node.name
  return node.constant_path.location.slice if node.respond_to?(:constant_path) && node.constant_path

  "anonymous"
end

def receiver_text(node)
  return nil unless node.respond_to?(:receiver) && node.receiver

  node.receiver.location.slice
end


walk = lambda do |node, scope|
  type = node.type.to_s
  next_scope = scope
  if %w[module_node class_node def_node].include?(type)
    name = node_name(node)
    kind = type == "module_node" ? "module" : type == "class_node" ? "class" : "method"
    qualified = (scope + [name]).join("::")
    parameters = if node.respond_to?(:parameters) && node.parameters
      node.parameters.location.slice.split(/\s*,\s*/).reject(&:empty?)
    else
      []
    end
    symbols << {
      "qualified_name" => qualified,
      "name" => name,
      "kind" => kind,
      "start_line" => node.location.start_line,
      "start_column" => node.location.start_column,
      "end_line" => node.location.end_line,
      "end_column" => node.location.end_column,
      "parameters" => parameters,
    }
    next_scope = scope + [name]
  elsif type == "call_node"
    name = node_name(node)
    receiver = receiver_text(node)
    identity = receiver ? "#{receiver}.#{name}" : name
    calls << {
      "identity" => identity,
      "owner" => scope.join("::"),
      "start_line" => node.location.start_line,
      "start_column" => node.location.start_column,
      "end_line" => node.location.end_line,
      "end_column" => node.location.end_column,
    }
    if %w[require require_relative load].include?(name) && node.respond_to?(:arguments) && node.arguments
      first = node.arguments.arguments.first
      if first && first.type.to_s == "string_node"
        imports << {
          "specifier" => first.unescaped,
          "kind" => name,
          "start_line" => node.location.start_line,
          "start_column" => node.location.start_column,
          "end_line" => node.location.end_line,
          "end_column" => node.location.end_column,
        }
      end
    end
    effect_names = %w[write binwrite delete unlink rename system spawn exec open start]
    effects << identity if effect_names.include?(name)
  elsif type == "hash_node"
    hashes << {
      "owner" => scope.join("::"),
      "start_line" => node.location.start_line,
      "end_line" => node.location.end_line,
    }
  elsif %w[rescue_node ensure_node raise_node].include?(type)
    exceptions << {
      "kind" => type.delete_suffix("_node"),
      "owner" => scope.join("::"),
      "start_line" => node.location.start_line,
    }
  end
  node.compact_child_nodes.each { |child| walk.call(child, next_scope) }
end

walk.call(result.value, [])
errors = result.errors.map do |error|
  {
    "message" => error.message,
    "start_line" => error.location.start_line,
    "start_column" => error.location.start_column,
  }
end

call_occurrences = CallOccurrences.serialize(result.value, source, result.errors, max_occurrences)

puts JSON.generate(
  {
    "prism_version" => Prism::VERSION,
    "symbols" => symbols.sort_by { |item| [item["qualified_name"], item["start_line"]] },
    "imports" => imports.sort_by { |item| [item["specifier"], item["start_line"]] },
    "calls" => calls.sort_by { |item| [item["owner"], item["start_line"], item["identity"]] },
    "hashes" => hashes.sort_by { |item| [item["owner"], item["start_line"]] },
    "exceptions" => exceptions.sort_by { |item| [item["owner"], item["start_line"]] },
    "effects" => effects.uniq.sort,
    "errors" => errors,
    "call_occurrences" => call_occurrences,
  }
)
