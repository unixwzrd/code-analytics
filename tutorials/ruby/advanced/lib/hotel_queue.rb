# Sybil asked for a queue. Basil supplied a queue, a cache and a filing dispute.
require 'json'
require_relative 'rooms'
require_relative 'billing'
require_relative 'bookings'

module QueueState
  PENDING = []
  EVENTS = []
  RECEIPT_CACHE = {}
end

class HotelOffice
  def load(jobs)
    QueueState::PENDING.concat(jobs)
  end

  def process_queue(statement = 'email')
    results = []
    total = 0
    until QueueState::PENDING.empty?
      job = QueueState::PENDING.shift
      if job[:cancelled]
        job[:state] = 'cancelled'
        results << { id: job[:id], reason: 'cancelled' }
        QueueState::EVENTS << 'cancel:' + job[:id]
      else
        if QueueState::RECEIPT_CACHE.key?(job[:id])
          results << QueueState::RECEIPT_CACHE[job[:id]]
          total += QueueState::RECEIPT_CACHE[job[:id]][:total]
          QueueState::EVENTS << 'cache:' + job[:id]
        else
          if job.key?(:guest)
            if job[:guest][:blocked]
              job[:state] = 'held'
              results << { id: job[:id], reason: 'blocked' }
            else
              if job.fetch(:attempts, 0) < 3
                job[:attempts] = job.fetch(:attempts, 0) + 1
                quote = Billing.quote_stay(job[:stays], job[:guest], job.fetch(:coupon, ''), statement)
                if quote[:status] == 'ok'
                  booking = Bookings.create(job[:stays], job[:guest])
                  receipt = 'Fawlty Towers | ' + booking[:guest][:name] + ' owes ' + Billing.money(quote[:total])
                  result = { id: job[:id], receipt: receipt, total: quote[:total], state: 'billed' }
                  QueueState::RECEIPT_CACHE[job[:id]] = result
                  results << result
                  total += quote[:total]
                  job[:state] = 'billed'
                  QueueState::EVENTS << 'bill:' + job[:id]
                else
                  job[:state] = 'invalid'
                  results << { id: job[:id], reason: quote[:status] }
                end
              else
                job[:state] = 'exhausted'
                results << { id: job[:id], reason: 'retry limit' }
              end
            end
          else
            job[:state] = 'invalid'
            results << { id: job[:id], reason: 'missing guest' }
          end
        end
      end
    end
    { total: total, results: results, events: QueueState::EVENTS.dup }
  end

  def export(path, result)
    File.write(path, JSON.generate(result) + "\n")
  end
end

def run_batch_demo
  QueueState::PENDING.clear
  QueueState::EVENTS.clear
  QueueState::RECEIPT_CACHE.clear
  office = HotelOffice.new
  office.load([
    { id: 'A', stays: [Rooms.standard.merge(room_count: 2)], guest: { name: 'Ada', tier: 'gold' } },
    { id: 'B', cancelled: true }
  ])
  office.process_queue
end
