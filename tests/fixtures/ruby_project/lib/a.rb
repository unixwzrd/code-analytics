module Demo
  class Writer
    def save(payload, path:)
      File.write(path, payload.to_json)
    rescue IOError
      raise
    ensure
      payload.clear
    end
  end
end
