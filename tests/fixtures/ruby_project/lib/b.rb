require_relative "a"

module Demo
  class Reader
    def load(path)
      value = File.read(path)
      {payload: value}
    end
  end
end
