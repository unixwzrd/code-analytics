# frozen_string_literal: true

# Bounded syntactic call/super/yield occurrence facts from an existing Prism parse tree.
# Does not execute or reload target source.

module CallOccurrences
  DEFAULT_MAX = 100_000

  SITE_KIND_RANK = {
    "call_node" => 0,
    "super_node" => 1,
    "forwarding_super_node" => 2,
    "yield_node" => 3,
  }.freeze

  LIMITATION_SYNTACTIC = "syntactic-context-is-not-runtime-execution-or-call-order"
  LIMITATION_CALLEE = "callee-spelling-does-not-establish-name-binding-or-dispatch"
  LIMITATION_OPERATOR_CALLEE =
    "operator-callee-spelling-is-not-representable-as-identifier-chain"
  LIMITATION_TRUNCATED = "truncated-at-max-occurrences"
  LIMITATION_PARSE_ERRORS = "parse-contains-errors"

  # Exact Ruby operator-method names from Prism call nodes; not identifier chains.
  KNOWN_OPERATOR_METHOD_NAMES = %w[
    [] []= + - * / % ** << >> & | ^ ~ +@ -@ <=> === == != <= >= < > ! =~ !~
  ].freeze

  module_function

  def serialize(root, source, errors, max_occurrences = DEFAULT_MAX)
    cap = normalize_cap(max_occurrences)
    collector = Collector.new(source, cap, errors)
    collector.collect(root)
    collector.to_h
  end

  def normalize_cap(value)
    unless value.is_a?(Integer) && value > 0
      raise ArgumentError, "max_occurrences must be a positive integer"
    end

    value
  end

  class Collector
    def initialize(source, max_occurrences, errors)
      @source = source
      @max_occurrences = max_occurrences
      @errors = errors
      @module_scope = scope_record("module", nil, 1, 0)
      @retained = []
      @total_observed = 0
      @traversal_ordinal = 0
      @limitations = [LIMITATION_SYNTACTIC, LIMITATION_CALLEE].sort.uniq
      @limitations << LIMITATION_PARSE_ERRORS unless errors.empty?
      @operator_method_call_sites = false
    end

    def collect(root)
      visit(root, frame([@module_scope], "module-body", "eager", 0))
    end

    def to_h
      ordered = @retained.sort_by { |entry| entry[:sort_key] }.map { |entry| entry[:fact] }
      omitted = @total_observed - ordered.length
      limitations = @limitations.dup
      limitations << LIMITATION_OPERATOR_CALLEE if @operator_method_call_sites
      limitations << LIMITATION_TRUNCATED if omitted.positive?
      limitations = limitations.sort.uniq
      {
        "occurrences" => ordered,
        "total_observed" => @total_observed,
        "total_retained" => ordered.length,
        "total_omitted" => omitted,
        "parse_complete" => @errors.empty?,
        "limitations" => limitations,
      }
    end

    private

    def frame(scope_path, evaluation_context, disposition, conditional_depth)
      {
        scope_path: scope_path,
        evaluation_context: evaluation_context,
        disposition: disposition,
        conditional_depth: conditional_depth,
      }
    end

    def visit(node, frame)
      type = node.type.to_s
      case type
      when "module_node"
        visit_module(node, frame)
      when "class_node"
        visit_class(node, frame)
      when "singleton_class_node"
        visit_singleton_class(node, frame)
      when "def_node"
        visit_def(node, frame)
      when "block_node"
        visit_block(node, frame)
      when "lambda_node"
        visit_lambda(node, frame)
      when "call_node", "super_node", "forwarding_super_node", "yield_node"
        visit_call_site(node, type, frame)
      when "if_node"
        visit_if(node, frame)
      when "unless_node"
        visit_unless(node, frame)
      when "while_node", "until_node"
        visit_while_until(node, frame)
      when "for_node"
        visit_for(node, frame)
      when "case_node"
        visit_case(node, frame)
      when "and_node", "or_node"
        visit_short_circuit(node, frame)
      when "rescue_node", "ensure_node"
        visit_children(node, with_conditional(frame))
      when "begin_node"
        visit_begin(node, frame)
      else
        visit_children(node, frame)
      end
    end

    def visit_module(node, frame)
      name = constant_name(node.constant_path)
      scope = push_scope(frame[:scope_path], scope_record("module", name, *def_position(node.constant_path)))
      visit_constant_path(node.constant_path, frame)
      inner = frame(scope, "module-body", frame[:disposition], frame[:conditional_depth])
      visit_statements(node.body, inner)
    end

    def visit_class(node, frame)
      name = constant_name(node.constant_path)
      scope = push_scope(frame[:scope_path], scope_record("class", name, *def_position(node.constant_path)))
      visit_constant_path(node.constant_path, frame)
      if node.superclass
        heritage = frame(scope, "class-heritage", frame[:disposition], frame[:conditional_depth])
        visit(node.superclass, heritage)
      end
      inner = frame(scope, "class-body", frame[:disposition], frame[:conditional_depth])
      visit_statements(node.body, inner)
    end

    def visit_singleton_class(node, frame)
      visit(node.expression, frame) if node.expression
      scope = push_scope(
        frame[:scope_path],
        scope_record("class", nil, node.location.start_line, node.location.start_column),
      )
      inner = frame(scope, "singleton-class-body", frame[:disposition], frame[:conditional_depth])
      visit_statements(node.body, inner)
    end

    def visit_def(node, frame)
      visit(node.receiver, frame) if node.respond_to?(:receiver) && node.receiver
      name = node.name ? node.name.to_s : nil
      name_loc = node.name_loc
      def_line = name_loc ? name_loc.start_line : node.location.start_line
      def_col = name_loc ? name_loc.start_column : node.location.start_column
      scope = push_scope(frame[:scope_path], scope_record("method", name, def_line, def_col))
      param_frame = frame(scope, "parameter-default", "deferred", frame[:conditional_depth] + 1)
      if node.parameters
        visit_parameters(node.parameters, param_frame)
      end
      body_frame = frame(scope, "method-body", "deferred", frame[:conditional_depth])
      visit_body(node.body, body_frame) if node.body
    end

    def visit_block(node, frame)
      scope = push_scope(
        frame[:scope_path],
        scope_record("block", nil, node.location.start_line, node.location.start_column),
      )
      inner = frame(scope, "block-body", "deferred", frame[:conditional_depth])
      if node.parameters
        visit_parameters(node.parameters, frame(scope, "parameter-default", "deferred", frame[:conditional_depth] + 1))
      end
      visit_body(node.body, inner) if node.body
    end

    def visit_lambda(node, frame)
      scope = push_scope(
        frame[:scope_path],
        scope_record("lambda", nil, node.location.start_line, node.location.start_column),
      )
      param_frame = frame(scope, "parameter-default", "deferred", frame[:conditional_depth])
      if node.parameters
        visit_parameters(node.parameters, param_frame)
      end
      inner = frame(scope, "lambda-body", "deferred", frame[:conditional_depth])
      visit_body(node.body, inner) if node.body
    end

    def visit_if(node, frame)
      visit(node.predicate, frame) if node.predicate
      branch = with_conditional(frame)
      visit_statements(node.statements, branch) if node.statements
      visit_if_consequent(node.consequent, frame) if node.consequent
    end

    def visit_if_consequent(node, frame)
      return unless node

      case node.type.to_s
      when "if_node"
        visit_if(node, frame)
      when "else_node"
        visit_statements(node.statements, frame) if node.statements
      else
        visit(node, frame)
      end
    end

    def visit_unless(node, frame)
      visit(node.predicate, frame) if node.predicate
      branch = with_conditional(frame)
      visit_statements(node.statements, branch) if node.statements
    end

    def visit_while_until(node, frame)
      visit(node.predicate, frame) if node.predicate
      branch = with_conditional(frame)
      visit_statements(node.statements, branch) if node.statements
    end

    def visit_for(node, frame)
      visit(node.collection, frame) if node.respond_to?(:collection) && node.collection
      branch = with_conditional(frame)
      visit_statements(node.statements, branch) if node.statements
    end

    def visit_case(node, frame)
      visit(node.predicate, frame) if node.predicate
      node.compact_child_nodes.each do |child|
        case child.type.to_s
        when "when_node"
          visit_when(child, frame)
        when "else_node"
          visit_statements(child.statements, frame) if child.statements
        end
      end
    end

    def visit_when(node, frame)
      branch = with_conditional(frame)
      if node.respond_to?(:conditions) && node.conditions
        node.conditions.each { |condition| visit(condition, branch) }
      end
      visit_statements(node.statements, branch) if node.statements
    end

    def visit_short_circuit(node, frame)
      visit(node.left, frame) if node.respond_to?(:left) && node.left
      visit(node.right, with_conditional(frame)) if node.respond_to?(:right) && node.right
    end

    def visit_call_site(node, site_kind, frame)
      record_site(node, site_kind, frame)
      child_frame = site_kind == "call_node" && safe_navigation_call?(node) ? with_conditional(frame) : frame
      if site_kind == "call_node"
        visit(node.receiver, frame) if node.receiver
        visit_arguments(node.arguments, child_frame) if node.arguments
        visit(node.block, child_frame) if node.block
      elsif node.respond_to?(:arguments) && node.arguments
        visit_arguments(node.arguments, child_frame)
      end
    end

    def visit_arguments(arguments_node, frame)
      arguments_node.compact_child_nodes.each { |child| visit(child, frame) }
    end

    def visit_body(body_node, frame)
      return unless body_node

      if body_node.type == :begin_node
        visit(body_node, frame)
      else
        visit_statements(body_node, frame)
      end
    end

    def visit_begin(node, frame)
      visit_statements(node.statements, frame) if node.statements
      visit(node.rescue_clause, with_conditional(frame)) if node.rescue_clause
      visit(node.ensure_clause, with_conditional(frame)) if node.ensure_clause
      visit(node.else_clause, frame) if node.else_clause
    end

    def visit_statements(statements_node, frame)
      return unless statements_node

      body = statements_node.body
      return unless body

      body.each do |statement|
        init_frame = initialization_frame(statement, frame)
        visit(statement, init_frame)
      end
    end

    def initialization_frame(statement, frame)
      type = statement.type.to_s
      return frame(scope_for_init(frame), "class-initialization", frame[:disposition], frame[:conditional_depth]) if %w[
        instance_variable_write_node class_variable_write_node
      ].include?(type)

      frame
    end

    def scope_for_init(frame)
      return frame[:scope_path] unless frame[:scope_path].empty?

      [scope_record("module", nil, 1, 0)]
    end

    def visit_parameters(parameters_node, frame)
      parameters_node.each_child_node { |child| visit(child, frame) }
    end

    def visit_constant_path(path, frame)
      return unless path

      visit(path, frame)
    end

    def visit_children(node, frame)
      node.compact_child_nodes.each { |child| visit(child, frame) }
    end

    def with_conditional(frame)
      frame.merge(conditional_depth: frame[:conditional_depth] + 1)
    end

    def safe_navigation_call?(node)
      node.respond_to?(:safe_navigation?) && node.safe_navigation?
    end

    def record_site(node, site_kind, frame)
      @total_observed += 1
      ordinal = @traversal_ordinal
      @traversal_ordinal += 1
      loc = node.location
      fact = {
        "site_kind" => site_kind,
        "start_byte" => loc.start_offset,
        "end_byte" => loc.end_offset,
        "start_line" => loc.start_line,
        "start_column" => loc.start_column,
        "end_line" => loc.end_line,
        "end_column" => loc.end_column,
        "callee_spelling" => callee_spelling(node, site_kind),
        "scope_path" => frame[:scope_path].map { |record| scope_json(record) },
        "evaluation_context" => frame[:evaluation_context],
        "execution_disposition" => effective_disposition(node, site_kind, frame),
        "conditional" => conditional_site?(node, site_kind, frame),
        "deferred" => deferred_site?(frame),
      }
      sort_key = [
        loc.start_offset,
        loc.start_line,
        loc.start_column,
        loc.end_offset,
        loc.end_column,
        SITE_KIND_RANK.fetch(site_kind, 99),
        ordinal,
      ]
      retain_entry(sort_key, fact)
    end

    def retain_entry(sort_key, fact)
      inverted = sort_key.map { |part| part.is_a?(Integer) ? -part : part }
      entry = { sort_key: sort_key, inverted: inverted, fact: fact }
      if @retained.length < @max_occurrences
        @retained << entry
        heapify_up(@retained.length - 1)
        return
      end
      worst = @retained[0]
      return unless compare_entries(entry, worst) > 0

      @retained[0] = entry
      heapify_down(0)
    end

    def compare_entries(a, b)
      a[:inverted].zip(b[:inverted]).each do |left, right|
        next if left == right

        return left <=> right
      end
      0
    end

    def heapify_up(index)
      while index.positive?
        parent = (index - 1) / 2
        break if compare_entries(@retained[index], @retained[parent]) >= 0

        @retained[index], @retained[parent] = @retained[parent], @retained[index]
        index = parent
      end
    end

    def heapify_down(index)
      loop do
        left = 2 * index + 1
        right = left + 1
        smallest = index
        smallest = left if left < @retained.length && compare_entries(@retained[left], @retained[smallest]) < 0
        smallest = right if right < @retained.length && compare_entries(@retained[right], @retained[smallest]) < 0
        break if smallest == index

        @retained[index], @retained[smallest] = @retained[smallest], @retained[index]
        index = smallest
      end
    end

    def effective_disposition(node, site_kind, frame)
      if site_kind == "call_node" && safe_navigation_call?(node)
        return "conditional"
      end

      return "conditional" if frame[:conditional_depth].positive?

      return "deferred" if deferred_site?(frame)

      frame[:disposition]
    end

    def conditional_site?(node, site_kind, frame)
      return true if frame[:conditional_depth].positive?
      return true if site_kind == "call_node" && safe_navigation_call?(node)

      false
    end

    def deferred_site?(frame)
      return true if frame[:disposition] == "deferred"

      %w[block-body lambda-body].include?(frame[:evaluation_context])
    end

    def callee_spelling(node, site_kind)
      return nil unless site_kind == "call_node"

      name = node.name
      name_str = name ? name.to_s : nil
      return nil if name_str.nil? || name_str.empty?

      if KNOWN_OPERATOR_METHOD_NAMES.include?(name_str)
        @operator_method_call_sites = true
        return nil
      end

      receiver = node.receiver
      if receiver.nil?
        return name_str
      end

      recv = safe_receiver_spelling(receiver)
      return nil if recv.nil?

      "#{recv}.#{name_str}"
    end

    def safe_receiver_spelling(node)
      type = node.type.to_s
      case type
      when "local_variable_read_node"
        node.name.to_s
      when "instance_variable_read_node"
        node.name.to_s.start_with?("@") ? node.name.to_s : "@#{node.name}"
      when "class_variable_read_node"
        node.name.to_s.start_with?("@@") ? node.name.to_s : "@@#{node.name}"
      when "global_variable_read_node"
        node.name.to_s.start_with?("$") ? node.name.to_s : "$#{node.name}"
      when "self_node"
        "self"
      when "constant_path_node"
        node.full_name
      end
    end

    def constant_name(path)
      path&.full_name
    end

    def def_position(path)
      return [1, 0] unless path

      loc = path.location
      [loc.start_line, loc.start_column]
    end

    def scope_record(kind, name, definition_line, definition_column)
      {
        kind: kind,
        name: name,
        definition_line: definition_line,
        definition_column: definition_column,
      }
    end

    def push_scope(path, record)
      path + [record]
    end

    def scope_json(record)
      {
        "kind" => record[:kind],
        "name" => record[:name],
        "definition_line" => record[:definition_line],
        "definition_column" => record[:definition_column],
      }
    end
  end
end
