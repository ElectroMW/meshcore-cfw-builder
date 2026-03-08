#include "MyMesh.h"

#include <algorithm>

#ifndef LORA_FREQ
#define LORA_FREQ 915.0
#endif
#ifndef LORA_BW
#define LORA_BW 250
#endif
#ifndef LORA_SF
#define LORA_SF 10
#endif
#ifndef LORA_CR
#define LORA_CR 5
#endif
#ifndef LORA_TX_POWER
#define LORA_TX_POWER 20
#endif

#ifndef ADVERT_NAME
#define ADVERT_NAME "repeater"
#endif
#ifndef ADVERT_LAT
#define ADVERT_LAT 0.0
#endif
#ifndef ADVERT_LON
#define ADVERT_LON 0.0
#endif

#ifndef ADMIN_PASSWORD
#define ADMIN_PASSWORD "password"
#endif

#ifndef SERVER_RESPONSE_DELAY
#define SERVER_RESPONSE_DELAY 300
#endif

#ifndef TXT_ACK_DELAY
#define TXT_ACK_DELAY 200
#endif

#define FIRMWARE_VER_LEVEL 2

#define REQ_TYPE_GET_STATUS 0x01
#define REQ_TYPE_KEEP_ALIVE 0x02
#define REQ_TYPE_GET_TELEMETRY_DATA 0x03
#define REQ_TYPE_GET_ACCESS_LIST 0x05
#define REQ_TYPE_GET_NEIGHBOURS 0x06
#define REQ_TYPE_GET_OWNER_INFO 0x07

#define ANON_REQ_TYPE_REGIONS 0x01
#define ANON_REQ_TYPE_OWNER 0x02
#define ANON_REQ_TYPE_BASIC 0x03  // just remote clock

#define RESP_SERVER_LOGIN_OK 0

#define CLI_REPLY_DELAY_MILLIS 600
#define LAZY_CONTACTS_WRITE_DELAY 5000

#define REPLY_DELAY_MILLIS 1500
#define PUSH_NOTIFY_DELAY_MILLIS 2000
#define SYNC_PUSH_INTERVAL 1200
#define PUSH_ACK_TIMEOUT_FLOOD 12000
#define PUSH_TIMEOUT_BASE 4000
#define PUSH_ACK_TIMEOUT_FACTOR 2000
#define POST_SYNC_DELAY_SECS 6

void MyMesh::putNeighbour(const mesh::Identity& id, uint32_t timestamp,
                          float snr) {
#if MAX_NEIGHBOURS
  uint32_t oldest_timestamp = 0xFFFFFFFF;
  NeighbourInfo* neighbour = &neighbours[0];
  for (int i = 0; i < MAX_NEIGHBOURS; i++) {
    if (id.matches(neighbours[i].id)) {
      neighbour = &neighbours[i];
      break;
    }
    if (neighbours[i].heard_timestamp < oldest_timestamp) {
      neighbour = &neighbours[i];
      oldest_timestamp = neighbour->heard_timestamp;
    }
  }
  neighbour->id = id;
  neighbour->advert_timestamp = timestamp;
  neighbour->heard_timestamp = getRTCClock()->getCurrentTime();
  neighbour->snr = (int8_t)(snr * 4);
#endif
}

void MyMesh::addPost(ClientInfo* client, const char* postData) {
  posts[next_post_idx].author = client->id;
  StrHelper::strncpy(posts[next_post_idx].text, postData, MAX_POST_TEXT_LEN);
  posts[next_post_idx].post_timestamp = getRTCClock()->getCurrentTimeUnique();
  next_post_idx = (next_post_idx + 1) % MAX_UNSYNCED_POSTS;

  next_push = futureMillis(PUSH_NOTIFY_DELAY_MILLIS);
  _num_posted++;
}

void MyMesh::pushPostToClient(ClientInfo* client, PostInfo& post) {
  int len = 0;
  memcpy(&reply_data[len], &post.post_timestamp, 4);
  len += 4;

  uint8_t attempt;
  getRNG()->random(&attempt, 1);
  reply_data[len++] = (TXT_TYPE_SIGNED_PLAIN << 2) | (attempt & 3);

  memcpy(&reply_data[len], post.author.pub_key, 4);
  len += 4;

  int text_len = strlen(post.text);
  memcpy(&reply_data[len], post.text, text_len);
  len += text_len;

  mesh::Utils::sha256((uint8_t*)&client->extra.room.pending_ack, 4, reply_data,
                      len, client->id.pub_key, PUB_KEY_SIZE);
  client->extra.room.push_post_timestamp = post.post_timestamp;

  mesh::LocalIdentity original_id = self_id;
  self_id = room_id;  // swap to room ID for sending

  mesh::Packet* pkt = createDatagram(PAYLOAD_TYPE_TXT_MSG, client->id,
                                     client->shared_secret, reply_data, len);

  self_id = original_id;

  if (pkt) {
    if (client->out_path_len < 0) {
      sendFlood(pkt);
      client->extra.room.ack_timeout = futureMillis(PUSH_ACK_TIMEOUT_FLOOD);
    } else {
      sendDirect(pkt, client->out_path, client->out_path_len);
      client->extra.room.ack_timeout =
          futureMillis(PUSH_TIMEOUT_BASE +
                       PUSH_ACK_TIMEOUT_FACTOR * (client->out_path_len + 1));
    }
    _num_post_pushes++;
    client->last_activity = millis();
  } else {
    client->extra.room.pending_ack = 0;
    MESH_DEBUG_PRINTLN("Unable to push post to client");
  }
}

uint8_t MyMesh::getUnsyncedCount(ClientInfo* client) {
  uint8_t count = 0;
  for (int k = 0; k < MAX_UNSYNCED_POSTS; k++) {
    if (posts[k].post_timestamp > client->extra.room.sync_since &&
        !posts[k].author.matches(client->id)) {
      count++;
    }
  }
  return count;
}

bool MyMesh::processAck(const uint8_t* data) {
  for (int i = 0; i < acl.getNumClients(); i++) {
    auto client = acl.getClientByIdx(i);
    if (client->extra.room.pending_ack &&
        memcmp(data, &client->extra.room.pending_ack, 4) == 0) {
      client->extra.room.pending_ack = 0;
      client->extra.room.push_failures = 0;
      client->extra.room.sync_since = client->extra.room.push_post_timestamp;
      return true;
    }
  }
  return false;
}

uint8_t MyMesh::handleAnonRegionsReq(const mesh::Identity& sender,
                                     uint32_t sender_timestamp,
                                     const uint8_t* data) {
  if (anon_limiter.allow(rtc_clock.getCurrentTime())) {
    // request data has: {reply-path-len}{reply-path}
    reply_path_len = *data++ & 0x3F;
    memcpy(reply_path, data, reply_path_len);
    // data += reply_path_len;

    memcpy(reply_data, &sender_timestamp,
           4);  // prefix with sender_timestamp, like a tag
    uint32_t now = getRTCClock()->getCurrentTime();
    memcpy(&reply_data[4], &now, 4);  // include our clock (for easy clock sync,
                                      // and packet hash uniqueness)

    return 8 + region_map.exportNamesTo((char*)&reply_data[8],
                                        sizeof(reply_data) - 12,
                                        REGION_DENY_FLOOD);  // reply length
  }
  return 0;
}

uint8_t MyMesh::handleAnonOwnerReq(const mesh::Identity& sender,
                                   uint32_t sender_timestamp,
                                   const uint8_t* data) {
  if (anon_limiter.allow(rtc_clock.getCurrentTime())) {
    // request data has: {reply-path-len}{reply-path}
    reply_path_len = *data++ & 0x3F;
    memcpy(reply_path, data, reply_path_len);
    // data += reply_path_len;

    memcpy(reply_data, &sender_timestamp,
           4);  // prefix with sender_timestamp, like a tag
    uint32_t now = getRTCClock()->getCurrentTime();
    memcpy(&reply_data[4], &now, 4);  // include our clock (for easy clock sync,
                                      // and packet hash uniqueness)
    sprintf((char*)&reply_data[8], "%s\n%s", _prefs.node_name,
            _prefs.owner_info);

    return 8 + strlen((char*)&reply_data[8]);  // reply length
  }
  return 0;
}

uint8_t MyMesh::handleAnonClockReq(const mesh::Identity& sender,
                                   uint32_t sender_timestamp,
                                   const uint8_t* data) {
  if (anon_limiter.allow(rtc_clock.getCurrentTime())) {
    // request data has: {reply-path-len}{reply-path}
    reply_path_len = *data++ & 0x3F;
    memcpy(reply_path, data, reply_path_len);
    // data += reply_path_len;

    memcpy(reply_data, &sender_timestamp,
           4);  // prefix with sender_timestamp, like a tag
    uint32_t now = getRTCClock()->getCurrentTime();
    memcpy(&reply_data[4], &now, 4);  // include our clock (for easy clock sync,
                                      // and packet hash uniqueness)
    reply_data[8] = 0;                // features
#ifdef WITH_RS232_BRIDGE
    reply_data[8] |= 0x01;  // is bridge, type UART
#elif WITH_ESPNOW_BRIDGE
    reply_data[8] |= 0x03;  // is bridge, type ESP-NOW
#endif
    if (_prefs.disable_fwd) {  // is this repeater currently disabled
      reply_data[8] |= 0x80;   // is disabled
    }
    // TODO:  add some kind of moving-window utilisation metric, so can query
    // 'how busy' is this repeater
    return 9;  // reply length
  }
  return 0;
}

uint8_t MyMesh::handleLoginReq(const mesh::Identity& sender,
                               const uint8_t* secret, uint32_t sender_timestamp,
                               const uint8_t* data, mesh::Packet* pkt,
                               bool is_room) {
  uint32_t sender_sync_since = 0;
  char* password = NULL;

  ClientInfo* client = NULL;
  uint8_t perm = 0;

  if (is_room) {
    memcpy(&sender_sync_since, &data[4], 4);
    password = (char*)(data + 8);
  } else {
    password = (char*)(data + 4);
  }

  if (password[0] == 0) {
    client = acl.getClient(sender.pub_key, PUB_KEY_SIZE);
  }

  if (client == NULL) {
    if (strcmp(password, _prefs.password) == 0) {
      perm = PERM_ACL_ADMIN;
    } else if (strcmp(password, _prefs.guest_password) == 0) {
      perm = PERM_ACL_READ_WRITE;  // Guest/Room User
    } else {
      if (is_room) {
        password = (char*)(data + 4);
        if (strcmp(password, _prefs.password) == 0) {
          perm = PERM_ACL_ADMIN;
          sender_sync_since = 0;  // repeater login?
        }
      }
    }
  }

  if (client == NULL && perm == 0) {
    return 0;
  }

  if (client == NULL) {
    client = acl.putClient(sender, 0);
    if (sender_timestamp <= client->last_timestamp) {
      return 0;
    }
    client->last_timestamp = sender_timestamp;
    client->extra.room.sync_since = sender_sync_since;
    client->extra.room.pending_ack = 0;
    client->extra.room.push_failures = 0;
    client->last_activity = getRTCClock()->getCurrentTime();
    client->permissions |= perm;
    memcpy(client->shared_secret, secret, PUB_KEY_SIZE);
    dirty_contacts_expiry = futureMillis(LAZY_CONTACTS_WRITE_DELAY);

    if (getRTCClock()->getCurrentTime() < sender_timestamp - 60) {
#if MESH_DEBUG
      MESH_DEBUG_PRINTLN("Updating Clock from Auth Login: %u -> %u",
                         getRTCClock()->getCurrentTime(), sender_timestamp);
#endif
      getRTCClock()->setCurrentTime(sender_timestamp);
    }
  }

  if (pkt->isRouteFlood()) {
    client->out_path_len = -1;
  }

  uint32_t now = getRTCClock()->getCurrentTimeUnique();
  memcpy(reply_data, &now, 4);
  reply_data[4] = RESP_SERVER_LOGIN_OK;
  reply_data[5] = 0;
  reply_data[6] = (client->isAdmin() ? 1 : 0);
  reply_data[7] = client->permissions;
  getRNG()->random(&reply_data[8], 4);
  reply_data[12] = FIRMWARE_VER_LEVEL;

  next_push = futureMillis(PUSH_NOTIFY_DELAY_MILLIS);

  return 13;  // reply length
}

int MyMesh::handleRequest(ClientInfo* sender, uint32_t sender_timestamp,
                          uint8_t* payload, size_t payload_len) {
  memcpy(reply_data, &sender_timestamp, 4);

  if (payload[0] == REQ_TYPE_GET_STATUS) {
    HybridStats stats;
    stats.batt_milli_volts = board.getBattMilliVolts();
    stats.curr_tx_queue_len = _mgr->getOutboundCount(0xFFFFFFFF);
    stats.noise_floor = (int16_t)_radio->getNoiseFloor();
    stats.last_rssi = (int16_t)radio_driver.getLastRSSI();
    stats.n_packets_recv = radio_driver.getPacketsRecv();
    stats.n_packets_sent = radio_driver.getPacketsSent();
    stats.total_air_time_secs = getTotalAirTime() / 1000;
    stats.total_up_time_secs = uptime_millis / 1000;
    stats.n_sent_flood = getNumSentFlood();
    stats.n_sent_direct = getNumSentDirect();
    stats.n_recv_flood = getNumRecvFlood();
    stats.n_recv_direct = getNumRecvDirect();
    stats.err_events = _err_flags;
    stats.last_snr = (int16_t)(radio_driver.getLastSNR() * 4);
    stats.n_direct_dups = ((SimpleMeshTables*)getTables())->getNumDirectDups();
    stats.n_flood_dups = ((SimpleMeshTables*)getTables())->getNumFloodDups();
    stats.total_rx_air_time_secs = getReceiveAirTime() / 1000;
    stats.n_recv_errors = radio_driver.getPacketsRecvErrors();
    stats.n_posted = _num_posted;
    stats.n_post_push = _num_post_pushes;

    memcpy(&reply_data[4], &stats, sizeof(stats));
    return 4 + sizeof(stats);
  }

  if (payload[0] == REQ_TYPE_GET_TELEMETRY_DATA) {
    uint8_t perm_mask = ~(payload[1]);

    telemetry.reset();
    telemetry.addVoltage(TELEM_CHANNEL_SELF,
                         (float)board.getBattMilliVolts() / 1000.0f);
    if ((sender->permissions & PERM_ACL_ROLE_MASK) == PERM_ACL_GUEST) {
      perm_mask = 0x00;
    }
    sensors.querySensors(perm_mask, telemetry);

    // This default temperature will be overridden by external sensors (if any)
    float temperature = board.getMCUTemperature();
    if (!isnan(temperature)) {  // Supported boards with built-in temperature
                                // sensor. ESP32-C3 may return NAN
      telemetry.addTemperature(TELEM_CHANNEL_SELF,
                               temperature);  // Built-in MCU Temperature
    }

    uint8_t tlen = telemetry.getSize();
    memcpy(&reply_data[4], telemetry.getBuffer(), tlen);
    return 4 + tlen;
  }

  if (sender->isAdmin()) {
    if (payload[0] == REQ_TYPE_GET_ACCESS_LIST) {
      uint8_t res1 = payload[1];
      uint8_t res2 = payload[2];
      if (res1 == 0 && res2 == 0) {
        uint8_t ofs = 4;
        for (int i = 0;
             i < acl.getNumClients() && ofs + 7 <= sizeof(reply_data) - 4;
             i++) {
          auto c = acl.getClientByIdx(i);
          if (c->permissions == 0) continue;
          memcpy(&reply_data[ofs], c->id.pub_key, 6);
          ofs += 6;
          reply_data[ofs++] = c->permissions;
        }
        return ofs;
      }
    }

    if (payload[0] == REQ_TYPE_GET_NEIGHBOURS) {
      uint8_t request_version = payload[1];
      if (request_version == 0) {
        int reply_offset = 4;
        uint8_t count = payload[2];
        uint16_t offset;
        memcpy(&offset, &payload[3], 2);
        uint8_t order_by = payload[5];
        uint8_t pubkey_prefix_length = payload[6];
        if (pubkey_prefix_length > PUB_KEY_SIZE)
          pubkey_prefix_length = PUB_KEY_SIZE;

        int16_t neighbours_count = 0;
        NeighbourInfo* sorted_neighbours[MAX_NEIGHBOURS];
        for (int i = 0; i < MAX_NEIGHBOURS; i++) {
          if (neighbours[i].heard_timestamp > 0) {
            sorted_neighbours[neighbours_count++] = &neighbours[i];
          }
        }

        if (order_by == 2) {  // Strongest
          std::sort(sorted_neighbours, sorted_neighbours + neighbours_count,
                    [](const NeighbourInfo* a, const NeighbourInfo* b) {
                      return a->snr > b->snr;
                    });
        } else if (order_by == 3) {  // Weakest
          std::sort(sorted_neighbours, sorted_neighbours + neighbours_count,
                    [](const NeighbourInfo* a, const NeighbourInfo* b) {
                      return a->snr < b->snr;
                    });
        } else {  // Time default
          std::sort(sorted_neighbours, sorted_neighbours + neighbours_count,
                    [](const NeighbourInfo* a, const NeighbourInfo* b) {
                      return a->heard_timestamp > b->heard_timestamp;
                    });
        }

        int results_count = 0;
        int results_offset = 0;
        uint8_t results_buffer[130];
        for (int index = 0; index < count && index + offset < neighbours_count;
             index++) {
          int entry_size = pubkey_prefix_length + 4 + 1;
          if (results_offset + entry_size > sizeof(results_buffer)) break;

          auto neighbour = sorted_neighbours[index + offset];
          uint32_t heard_seconds_ago =
              getRTCClock()->getCurrentTime() - neighbour->heard_timestamp;
          memcpy(&results_buffer[results_offset], neighbour->id.pub_key,
                 pubkey_prefix_length);
          results_offset += pubkey_prefix_length;
          memcpy(&results_buffer[results_offset], &heard_seconds_ago, 4);
          results_offset += 4;
          memcpy(&results_buffer[results_offset], &neighbour->snr, 1);
          results_offset += 1;
          results_count++;
        }

        memcpy(&reply_data[reply_offset], &neighbours_count, 2);
        reply_offset += 2;
        memcpy(&reply_data[reply_offset], &results_count, 2);
        reply_offset += 2;
        memcpy(&reply_data[reply_offset], &results_buffer, results_offset);
        reply_offset += results_offset;
        return reply_offset;
      }
    } else if (payload[0] == REQ_TYPE_GET_OWNER_INFO) {
      sprintf((char*)&reply_data[4], "%s\n%s\n%s", FIRMWARE_VERSION,
              _prefs.node_name, _prefs.owner_info);
      return 4 + strlen((char*)&reply_data[4]);
    }
  }

  return 0;  // unknown
}

mesh::Packet* MyMesh::createSelfAdvert() {
  uint8_t app_data[MAX_ADVERT_DATA_SIZE];

  uint8_t app_data_len = _cli.buildAdvertData(ADV_TYPE_REPEATER, app_data);
  return createAdvert(self_id, app_data, app_data_len);
}

mesh::Packet* MyMesh::createRoomAdvert() {
  uint8_t app_data[MAX_ADVERT_DATA_SIZE];

  char room_name[32];
  snprintf(room_name, sizeof(room_name), "%s [Room]", _prefs.node_name);

  if (_prefs.node_lat != 0.0 || _prefs.node_lon != 0.0) {
    AdvertDataBuilder builder(ADV_TYPE_ROOM, room_name, _prefs.node_lat,
                              _prefs.node_lon);
    uint8_t app_data_len = builder.encodeTo(app_data);
    app_data[0] = (app_data[0] & 0xF0) | ADV_TYPE_ROOM;
    return createAdvert(room_id, app_data, app_data_len);
  } else {
    AdvertDataBuilder builder(ADV_TYPE_ROOM, room_name);
    uint8_t app_data_len = builder.encodeTo(app_data);
    app_data[0] = (app_data[0] & 0xF0) | ADV_TYPE_ROOM;
    return createAdvert(room_id, app_data, app_data_len);
  }
}

void MyMesh::sendSelfAdvertisement(int delay_millis, bool flood) {
  mesh::Packet* pkt = createSelfAdvert();
  if (pkt) {
    if (flood) {
      sendFlood(pkt, delay_millis);
    } else {
      sendZeroHop(pkt, delay_millis);
    }
    // schedule room advert 4 seconds later (chained after this one)
    next_room_advert = futureMillis(delay_millis + 4000);
  } else {
    MESH_DEBUG_PRINTLN("ERROR: unable to create advertisement packet!");
  }
}

void MyMesh::sendRoomAdvertisement(int delay_millis) {
  mesh::Packet* pkt = createRoomAdvert();
  if (pkt) {
    sendFlood(pkt, delay_millis);
  } else {
    MESH_DEBUG_PRINTLN("ERROR: unable to create room advertisement packet!");
  }
}

File MyMesh::openAppend(const char* fname) {
#if defined(NRF52_PLATFORM) || defined(STM32_PLATFORM)
  return _fs->open(fname, FILE_O_WRITE);
#elif defined(RP2040_PLATFORM)
  return _fs->open(fname, "a");
#else
  return _fs->open(fname, "a", true);
#endif
}

bool MyMesh::allowPacketForward(const mesh::Packet* packet) {
  if (_prefs.disable_fwd) return false;

  if (packet->isRouteFlood() && packet->path_len >= _prefs.flood_max)
    return false;

  if (packet->isRouteFlood() && recv_pkt_region == NULL) {
    return false;
  }
  return true;
}

const char* MyMesh::getLogDateTime() {
  static char tmp[32];
  uint32_t now = getRTCClock()->getCurrentTime();
  DateTime dt = DateTime(now);
  sprintf(tmp, "%02d:%02d:%02d - %d/%d/%d U", dt.hour(), dt.minute(),
          dt.second(), dt.day(), dt.month(), dt.year());
  return tmp;
}

void MyMesh::logRxRaw(float snr, float rssi, const uint8_t raw[], int len) {
#if MESH_PACKET_LOGGING
  Serial.print(getLogDateTime());
  Serial.print(" RAW: ");
  mesh::Utils::printHex(Serial, raw, len);
  Serial.println();
#endif
}

void MyMesh::logRx(mesh::Packet* pkt, int len, float score) {
#ifdef WITH_BRIDGE
  if (_prefs.bridge_pkt_src == 1) bridge.sendPacket(pkt);
#endif
  if (_logging) {
    File f = openAppend(PACKET_LOG_FILE);
    if (f) {
      f.print(getLogDateTime());
      f.printf(
          ": RX, len=%d (type=%d, route=%s, payload_len=%d) SNR=%d RSSI=%d "
          "score=%d\n",
          len, pkt->getPayloadType(), pkt->isRouteDirect() ? "D" : "F",
          pkt->payload_len, (int)_radio->getLastSNR(),
          (int)_radio->getLastRSSI(), (int)(score * 1000));
      f.close();
    }
  }
}

void MyMesh::logTx(mesh::Packet* pkt, int len) {
#ifdef WITH_BRIDGE
  if (_prefs.bridge_pkt_src == 0) bridge.sendPacket(pkt);
#endif
  if (_logging) {
    File f = openAppend(PACKET_LOG_FILE);
    if (f) {
      f.print(getLogDateTime());
      f.printf(": TX, len=%d (type=%d, route=%s, payload_len=%d)\n", len,
               pkt->getPayloadType(), pkt->isRouteDirect() ? "D" : "F",
               pkt->payload_len);
      f.close();
    }
  }
}

void MyMesh::logTxFail(mesh::Packet* pkt, int len) {
  if (_logging) {
    File f = openAppend(PACKET_LOG_FILE);
    if (f) {
      f.print(getLogDateTime());
      f.printf(": TX FAIL!, len=%d\n", len);
      f.close();
    }
  }
}

int MyMesh::calcRxDelay(float score, uint32_t air_time) const {
  if (_prefs.rx_delay_base <= 0.0f) return 0;
  return (int)((pow(_prefs.rx_delay_base, 0.85f - score) - 1.0) * air_time);
}

uint32_t MyMesh::getRetransmitDelay(const mesh::Packet* packet) {
  uint32_t t =
      (_radio->getEstAirtimeFor(packet->path_len + packet->payload_len + 2) *
       _prefs.tx_delay_factor);
  return getRNG()->nextInt(0, 5 * t + 1);
}
uint32_t MyMesh::getDirectRetransmitDelay(const mesh::Packet* packet) {
  uint32_t t =
      (_radio->getEstAirtimeFor(packet->path_len + packet->payload_len + 2) *
       _prefs.direct_tx_delay_factor);
  return getRNG()->nextInt(0, 5 * t + 1);
}

bool MyMesh::filterRecvFloodPacket(mesh::Packet* pkt) {
  if (pkt->getRouteType() == ROUTE_TYPE_TRANSPORT_FLOOD) {
    recv_pkt_region = region_map.findMatch(pkt, REGION_DENY_FLOOD);
  } else if (pkt->getRouteType() == ROUTE_TYPE_FLOOD) {
    if (region_map.getWildcard().flags & REGION_DENY_FLOOD)
      recv_pkt_region = NULL;
    else
      recv_pkt_region = &region_map.getWildcard();
  } else {
    recv_pkt_region = NULL;
  }
  return false;
}

void MyMesh::onAnonDataRecv(mesh::Packet* pkt, const uint8_t* secret,
                            const mesh::Identity& sender, uint8_t* data,
                            size_t len) {
  if (pkt->getPayloadType() == PAYLOAD_TYPE_ANON_REQ) {
    uint32_t timestamp;
    memcpy(&timestamp, data, 4);

    data[len] = 0;  // ensure null terminator
    uint8_t reply_len;

    bool is_room = room_id.isHashMatch(&pkt->payload[0]);

    reply_path_len = -1;
    if (is_room || (data[4] == 0 || data[4] >= ' ')) {
      reply_len =
          handleLoginReq(sender, secret, timestamp, &data[0], pkt, is_room);
    } else if (data[4] == ANON_REQ_TYPE_REGIONS && pkt->isRouteDirect()) {
      reply_len = handleAnonRegionsReq(sender, timestamp, &data[5]);
    } else if (data[4] == ANON_REQ_TYPE_OWNER && pkt->isRouteDirect()) {
      reply_len = handleAnonOwnerReq(sender, timestamp, &data[5]);
    } else if (data[4] == ANON_REQ_TYPE_BASIC && pkt->isRouteDirect()) {
      reply_len = handleAnonClockReq(sender, timestamp, &data[5]);
    } else {
      reply_len = 0;  // unknown/invalid request type
    }

    if (reply_len == 0) return;  // invalid request

    if (pkt->isRouteFlood()) {
      // let this sender know path TO here, so they can use sendDirect(), and
      // ALSO encode the response
      mesh::Packet* path =
          createPathReturn(sender, secret, pkt->path, pkt->path_len,
                           PAYLOAD_TYPE_RESPONSE, reply_data, reply_len);
      if (path) sendFlood(path, SERVER_RESPONSE_DELAY);
    } else if (reply_path_len < 0) {
      mesh::Packet* reply = createDatagram(PAYLOAD_TYPE_RESPONSE, sender,
                                           secret, reply_data, reply_len);
      if (reply) sendFlood(reply, SERVER_RESPONSE_DELAY);
    } else {
      mesh::Packet* reply = createDatagram(PAYLOAD_TYPE_RESPONSE, sender,
                                           secret, reply_data, reply_len);
      if (reply)
        sendDirect(reply, reply_path, reply_path_len, SERVER_RESPONSE_DELAY);
    }
  }
}

int MyMesh::searchPeersByHash(const uint8_t* hash) {
  int n = 0;
  for (int i = 0; i < acl.getNumClients(); i++) {
    if (acl.getClientByIdx(i)->id.isHashMatch(hash)) {
      MESH_DEBUG_PRINTLN("searchPeersByHash: Found match at idx %d hash %02X",
                         i, hash[0]);
      matching_peer_indexes[n++] = i;
    }
  }
  if (n == 0)
    MESH_DEBUG_PRINTLN("searchPeersByHash: NO MATCH for hash %02X", hash[0]);
  return n;
}

void MyMesh::getPeerSharedSecret(uint8_t* dest_secret, int peer_idx) {
  int i = matching_peer_indexes[peer_idx];
  if (i >= 0 && i < acl.getNumClients()) {
    memcpy(dest_secret, acl.getClientByIdx(i)->shared_secret, PUB_KEY_SIZE);
  }
}

static bool isShare(const mesh::Packet* packet) {
  if (packet->hasTransportCodes()) {
    return packet->transport_codes[0] == 0 && packet->transport_codes[1] == 0;
  }
  return false;
}

void MyMesh::onAdvertRecv(mesh::Packet* packet, const mesh::Identity& id,
                          uint32_t timestamp, const uint8_t* app_data,
                          size_t app_data_len) {
  mesh::Mesh::onAdvertRecv(packet, id, timestamp, app_data, app_data_len);

  if (packet->path_len == 0 && !isShare(packet)) {
    AdvertDataParser parser(app_data, app_data_len);
    if (parser.isValid() && parser.getType() == ADV_TYPE_REPEATER) {
      putNeighbour(id, timestamp, packet->getSNR());
    }
  }
}

void MyMesh::onPeerDataRecv(mesh::Packet* packet, uint8_t type, int sender_idx,
                            const uint8_t* secret, uint8_t* data, size_t len) {
  int i = matching_peer_indexes[sender_idx];
  if (i < 0 || i >= acl.getNumClients()) {
    return;
  }
  auto client = acl.getClientByIdx(i);
  if (type == PAYLOAD_TYPE_TXT_MSG && len > 5) {
    uint32_t sender_timestamp;
    memcpy(&sender_timestamp, data, 4);
    uint8_t flags = (data[4] >> 2);

    if (!(flags == TXT_TYPE_PLAIN || flags == TXT_TYPE_CLI_DATA)) {
      MESH_DEBUG_PRINTLN(
          "onPeerDataRecv: unsupported command flags received: flags=%02x",
          (uint32_t)flags);
    } else if (sender_timestamp >= client->last_timestamp) {
      bool is_retry = (sender_timestamp == client->last_timestamp);
      client->last_timestamp = sender_timestamp;

      uint32_t now = getRTCClock()->getCurrentTimeUnique();
      client->last_activity = now;
      client->extra.room.push_failures = 0;

      data[len] = 0;

      uint32_t ack_hash;
      mesh::Utils::sha256((uint8_t*)&ack_hash, 4, data,
                          5 + strlen((char*)&data[5]), client->id.pub_key,
                          PUB_KEY_SIZE);

      uint8_t temp[166];
      bool send_ack;
      if (flags == TXT_TYPE_CLI_DATA) {
        if (client->isAdmin()) {
          if (is_retry) {
            temp[5] = 0;
          } else {
            handleCommand(sender_timestamp, (char*)&data[5], (char*)&temp[5]);
            temp[4] = (TXT_TYPE_CLI_DATA << 2);
          }
          send_ack = false;
        } else {
          temp[5] = 0;
          send_ack = false;
        }
      } else {
        if ((client->permissions & PERM_ACL_ROLE_MASK) == PERM_ACL_GUEST) {
          temp[5] = 0;
          send_ack = false;
        } else {
          if (!is_retry) {
            addPost(client, (const char*)&data[5]);
          }
          temp[5] = 0;
          send_ack = true;
        }
      }

      uint32_t delay_millis;
      if (send_ack) {
        if (client->out_path_len < 0) {
          mesh::Packet* ack = createAck(ack_hash);
          if (ack) sendFlood(ack, 300);
          delay_millis = 300 + 500;
        } else {
          uint32_t d = 300;
          if (getExtraAckTransmitCount() > 0) {
            mesh::Packet* a1 = createMultiAck(ack_hash, 1);
            if (a1) sendDirect(a1, client->out_path, client->out_path_len, d);
            d += 300;
          }

          mesh::Packet* a2 = createAck(ack_hash);
          if (a2) sendDirect(a2, client->out_path, client->out_path_len, d);
          delay_millis = d + 500;
        }
      } else {
        delay_millis = 0;
      }

      int text_len = strlen((char*)&temp[5]);
      if (text_len > 0) {
        if (now == sender_timestamp) {
          // WORKAROUND: the two timestamps need to be different, in the CLI
          // view
          now++;
        }
        memcpy(temp, &now, 4);

        auto reply = createDatagram(PAYLOAD_TYPE_TXT_MSG, client->id, secret,
                                    temp, 5 + text_len);
        if (reply) {
          if (client->out_path_len < 0) {
            sendFlood(reply, delay_millis + 500);
          } else {
            sendDirect(reply, client->out_path, client->out_path_len,
                       delay_millis + 500);
          }
        }
      }
    } else {
      MESH_DEBUG_PRINTLN("onPeerDataRecv: possible replay attack detected");
    }
  }

  if (type == PAYLOAD_TYPE_REQ) {
    uint32_t sender_timestamp;
    memcpy(&sender_timestamp, data, 4);

    if (sender_timestamp < client->last_timestamp) {
      return;
    }
    client->last_timestamp = sender_timestamp;
    client->last_activity = getRTCClock()->getCurrentTime();
    client->extra.room.push_failures = 0;

    if (data[4] == REQ_TYPE_KEEP_ALIVE && packet->isRouteDirect()) {
      uint32_t forceSince = 0;
      if (len >= 9)
        memcpy(&forceSince, &data[5], 4);
      else
        memcpy(&data[5], &forceSince, 4);

      if (forceSince > 0) client->extra.room.sync_since = forceSince;
      client->extra.room.pending_ack = 0;

      if (client->out_path_len >= 0) {
        uint32_t ack_hash;
        mesh::Utils::sha256((uint8_t*)&ack_hash, 4, data, 9, client->id.pub_key,
                            PUB_KEY_SIZE);
        auto reply = createAck(ack_hash);
        if (reply) {
          reply->payload[reply->payload_len++] = getUnsyncedCount(client);
          sendDirect(reply, client->out_path, client->out_path_len,
                     SERVER_RESPONSE_DELAY);
        }
      }
    } else {
      int reply_len =
          handleRequest(client, sender_timestamp, &data[4], len - 4);
      if (reply_len > 0) {
        if (packet->isRouteFlood()) {
          mesh::Packet* path = createPathReturn(
              client->id, secret, packet->path, packet->path_len,
              PAYLOAD_TYPE_RESPONSE, reply_data, reply_len);
          if (path) sendFlood(path, SERVER_RESPONSE_DELAY);
        } else {
          mesh::Packet* reply = createDatagram(
              PAYLOAD_TYPE_RESPONSE, client->id, secret, reply_data, reply_len);
          if (reply) {
            if (client->out_path_len >= 0)
              sendDirect(reply, client->out_path, client->out_path_len,
                         SERVER_RESPONSE_DELAY);
            else
              sendFlood(reply, SERVER_RESPONSE_DELAY);
          }
        }
      }
    }
  } else if (type == PAYLOAD_TYPE_TXT_MSG && len > 5) {
    uint32_t sender_timestamp;
    memcpy(&sender_timestamp, data, 4);
    uint8_t flags = (data[4] >> 2);
    if (sender_timestamp >= client->last_timestamp) {
      bool is_retry = (sender_timestamp == client->last_timestamp);
      client->last_timestamp = sender_timestamp;
      client->last_activity = getRTCClock()->getCurrentTimeUnique();
      client->extra.room.push_failures = 0;

      if (getRTCClock()->getCurrentTime() < sender_timestamp - 60) {
        MESH_DEBUG_PRINTLN("Updating Clock from Auth Message: %u -> %u",
                           getRTCClock()->getCurrentTime(), sender_timestamp);
        getRTCClock()->setCurrentTime(sender_timestamp);
      }

      data[len] = 0;

      uint32_t ack_hash;
      mesh::Utils::sha256((uint8_t*)&ack_hash, 4, data,
                          5 + strlen((char*)&data[5]), client->id.pub_key,
                          PUB_KEY_SIZE);

      uint8_t temp[166];
      bool send_ack = false;

      if (flags == TXT_TYPE_CLI_DATA) {
        if (client->isAdmin()) {
          if (!is_retry) {
            handleCommand(sender_timestamp, (char*)&data[5], (char*)&temp[5]);
            temp[4] = (TXT_TYPE_CLI_DATA << 2);
            int text_len = strlen((char*)&temp[5]);
            if (text_len > 0) {
              uint32_t now = getRTCClock()->getCurrentTimeUnique();
              if (now == sender_timestamp) now++;
              memcpy(temp, &now, 4);
              auto reply = createDatagram(PAYLOAD_TYPE_TXT_MSG, client->id,
                                          secret, temp, 5 + text_len);
              if (reply) {
                if (client->out_path_len < 0)
                  sendFlood(reply, CLI_REPLY_DELAY_MILLIS);
                else
                  sendDirect(reply, client->out_path, client->out_path_len,
                             CLI_REPLY_DELAY_MILLIS);
              }
            }
          }
        }
      } else if (flags == TXT_TYPE_PLAIN) {
        if ((client->permissions & PERM_ACL_ROLE_MASK) != PERM_ACL_GUEST &&
            !client->isAdmin()) {
          MESH_DEBUG_PRINTLN("  -> Permission Denied! (perm=%u)",
                             client->permissions);
        } else {
          if (!is_retry) {
            addPost(client, (const char*)&data[5]);
          }
          send_ack = true;
        }
      }

      if (send_ack) {
        if (client->out_path_len < 0) {
          mesh::Packet* ack = createAck(ack_hash);
          if (ack) sendFlood(ack, TXT_ACK_DELAY);
        } else {
          if (getExtraAckTransmitCount() > 0) {
            mesh::Packet* a1 = createMultiAck(ack_hash, 1);
            if (a1)
              sendDirect(a1, client->out_path, client->out_path_len,
                         TXT_ACK_DELAY);
          }
          mesh::Packet* a2 = createAck(ack_hash);
          if (a2)
            sendDirect(a2, client->out_path, client->out_path_len,
                       TXT_ACK_DELAY + 300);
        }
      }
    }
  }
}

bool MyMesh::onPeerPathRecv(mesh::Packet* packet, int sender_idx,
                            const uint8_t* secret, uint8_t* path,
                            uint8_t path_len, uint8_t extra_type,
                            uint8_t* extra, uint8_t extra_len) {
  int i = matching_peer_indexes[sender_idx];
  if (i >= 0 && i < acl.getNumClients()) {
    auto client = acl.getClientByIdx(i);
    memcpy(client->out_path, path, client->out_path_len = path_len);
    client->last_activity = getRTCClock()->getCurrentTime();

    if (extra_type == PAYLOAD_TYPE_ACK && extra_len >= 4) {
      processAck(extra);
    }
  }
  return false;
}

void MyMesh::onAckRecv(mesh::Packet* packet, uint32_t ack_crc) {
  if (processAck((uint8_t*)&ack_crc)) {
    packet->markDoNotRetransmit();
  }
}

#define CTL_TYPE_NODE_DISCOVER_REQ 0x80
#define CTL_TYPE_NODE_DISCOVER_RESP 0x90

void MyMesh::onControlDataRecv(mesh::Packet* packet) {
  uint8_t type = packet->payload[0] & 0xF0;
  if (type == CTL_TYPE_NODE_DISCOVER_REQ && packet->payload_len >= 6 &&
      !_prefs.disable_fwd &&
      discover_limiter.allow(rtc_clock.getCurrentTime())) {
    int i = 1;
    uint8_t filter = packet->payload[i++];
    uint32_t tag;
    memcpy(&tag, &packet->payload[i], 4);
    i += 4;

    if ((filter & (1 << ADV_TYPE_REPEATER)) != 0) {
      bool prefix_only = packet->payload[0] & 1;
      uint8_t data[6 + PUB_KEY_SIZE];
      data[0] = 0x90 | ADV_TYPE_REPEATER;
      data[1] = packet->_snr;
      memcpy(&data[2], &tag, 4);
      memcpy(&data[6], self_id.pub_key, PUB_KEY_SIZE);
      auto resp =
          createControlData(data, prefix_only ? 6 + 8 : 6 + PUB_KEY_SIZE);
      if (resp) {
        sendZeroHop(resp, getRetransmitDelay(resp) * 4);
      }
    }
  }
}

MyMesh::MyMesh(mesh::MainBoard& board, mesh::Radio& radio,
               mesh::MillisecondClock& ms, mesh::RNG& rng, mesh::RTCClock& rtc,
               mesh::MeshTables& tables)
    : mesh::Mesh(radio, ms, rng, rtc, *new StaticPoolPacketManager(32), tables),
      _cli(board, rtc, sensors, acl, &_prefs, this),
      telemetry(MAX_PACKET_PAYLOAD - 4),
      region_map(key_store),
      temp_map(key_store),
      discover_limiter(4, 120),
      anon_limiter(4, 180)  // max 4 every 3 minutes
#if defined(WITH_RS232_BRIDGE)
      ,
      bridge(&_prefs, WITH_RS232_BRIDGE, _mgr, &rtc)
#endif
#if defined(WITH_ESPNOW_BRIDGE)
      ,
      bridge(&_prefs, _mgr, &rtc)
#endif
{
  last_millis = 0;
  uptime_millis = 0;
  next_local_advert = next_flood_advert = 0;
  dirty_contacts_expiry = 0;
  set_radio_at = revert_radio_at = 0;
  _logging = false;

#if MAX_NEIGHBOURS
  memset(neighbours, 0, sizeof(neighbours));
#endif
  region_load_active = false;

  memset(&_prefs, 0, sizeof(_prefs));
  _prefs.airtime_factor = 1.0;
  _prefs.rx_delay_base = 0.0f;
  _prefs.tx_delay_factor = 0.5f;
  _prefs.direct_tx_delay_factor = 0.2f;
  StrHelper::strncpy(_prefs.node_name, ADVERT_NAME, sizeof(_prefs.node_name));
  _prefs.node_lat = ADVERT_LAT;
  _prefs.node_lon = ADVERT_LON;
  StrHelper::strncpy(_prefs.password, ADMIN_PASSWORD, sizeof(_prefs.password));
  _prefs.freq = LORA_FREQ;
  _prefs.sf = LORA_SF;
  _prefs.bw = LORA_BW;
  _prefs.cr = LORA_CR;
  _prefs.tx_power_dbm = LORA_TX_POWER;
  _prefs.advert_interval = 1;
  _prefs.flood_advert_interval = 12;
  _prefs.flood_max = 64;
  _prefs.interference_threshold = 0;

#ifdef ROOM_PASSWORD
  StrHelper::strncpy(_prefs.guest_password, ROOM_PASSWORD,
                     sizeof(_prefs.guest_password));
#endif
  next_post_idx = 0;
  next_client_idx = 0;
  next_push = 0;
  memset(posts, 0, sizeof(posts));
  _num_posted = _num_post_pushes = 0;
  next_room_advert = 0;

  _prefs.bridge_enabled = 1;
  _prefs.bridge_delay = 500;
  _prefs.bridge_pkt_src = 0;
  _prefs.bridge_baud = 115200;
  _prefs.bridge_channel = 1;
  StrHelper::strncpy(_prefs.bridge_secret, "LVSITANOS",
                     sizeof(_prefs.bridge_secret));

  _prefs.gps_enabled = 0;
  _prefs.gps_interval = 0;
  _prefs.advert_loc_policy = ADVERT_LOC_PREFS;
  _prefs.adc_multiplier = 0.0f;
}

void MyMesh::begin(FILESYSTEM* fs) {
  mesh::Mesh::begin();
  _fs = fs;
  _cli.loadPrefs(_fs);
  acl.load(_fs, self_id);
  region_map.load(_fs);

#if defined(WITH_BRIDGE)
  if (_prefs.bridge_enabled) bridge.begin();
#endif

  radio_set_params(_prefs.freq, _prefs.bw, _prefs.sf, _prefs.cr);
  radio_set_tx_power(_prefs.tx_power_dbm);

  updateAdvertTimer();
  updateFloodAdvertTimer();
  board.setAdcMultiplier(_prefs.adc_multiplier);

#if ENV_INCLUDE_GPS == 1
  applyGpsPrefs();
#endif

  const char* id_path = "";
#if defined(ESP32) || defined(RP2040_PLATFORM)
  id_path = "/identity";
#endif

  IdentityStore store(*_fs, id_path);
  if (!store.load("_room", room_id)) {
    room_id = mesh::LocalIdentity(getRNG());
    store.save("_room", room_id);
    MESH_DEBUG_PRINTLN("Generated new Room ID");
  } else {
    MESH_DEBUG_PRINTLN("Loaded Room ID");
  }

  MESH_DEBUG_PRINT("Self ID: ");
  mesh::Utils::printHex(Serial, self_id.pub_key, 6);
  MESH_DEBUG_PRINTLN("");
  MESH_DEBUG_PRINT("Room ID: ");
  mesh::Utils::printHex(Serial, room_id.pub_key, 6);
  MESH_DEBUG_PRINTLN("");
  MESH_DEBUG_PRINT("Room Hash: ");
  uint8_t h;
  room_id.copyHashTo(&h);
  mesh::Utils::printHex(Serial, &h, 1);
  MESH_DEBUG_PRINTLN("");
}

mesh::DispatcherAction MyMesh::onRecvPacket(mesh::Packet* pkt) {
  bool swapped = false;
  mesh::LocalIdentity original_id = self_id;

  MESH_DEBUG_PRINTLN("Recv type=%d route=%d path_len=%d", pkt->getPayloadType(),
                     pkt->getRouteType(), pkt->path_len);

  // check if packet is addressed to our room identity
  if (pkt->getPayloadType() == PAYLOAD_TYPE_TXT_MSG ||
      pkt->getPayloadType() == PAYLOAD_TYPE_REQ ||
      pkt->getPayloadType() == PAYLOAD_TYPE_RESPONSE ||
      pkt->getPayloadType() == PAYLOAD_TYPE_PATH ||
      pkt->getPayloadType() == PAYLOAD_TYPE_ANON_REQ ||
      pkt->getPayloadType() == 9) {  // 9 = PING

    bool match = false;
    if (pkt->getPayloadType() == 9) {
      // PING has the target hash at the END of the payload (at least for 0-hop
      // pings in this mesh)
      if (pkt->payload_len > 0 &&
          room_id.isHashMatch(&pkt->payload[pkt->payload_len - 1])) {
        match = true;
      }
    } else {
      if (room_id.isHashMatch(&pkt->payload[0])) {
        match = true;
      }
    }

    if (match) {
      MESH_DEBUG_PRINTLN("Target ID matches Room Identity (Payload)!");
      self_id = room_id;
      swapped = true;
    }
  }

  // also check direct route path (header) match
  if (!swapped && pkt->isRouteDirect() && pkt->path_len > 0) {
    if (room_id.isHashMatch(pkt->path)) {
      MESH_DEBUG_PRINTLN("Target ID matches Room Identity (Path)!");
      self_id = room_id;
      swapped = true;
    }
  }

  if (pkt->isRouteDirect() && pkt->path_len > 0) {
    // check against current self_id (which might be swapped to room_id already)
    if (self_id.isHashMatch(pkt->path)) {
      MESH_DEBUG_PRINTLN(
          "Direct Packet for me! Stripping path to force consumption.");

      // it's for us!
      pkt->path_len -= PATH_HASH_SIZE;
      for (int k = 0; k < pkt->path_len; k++) {
        pkt->path[k] = pkt->path[k + PATH_HASH_SIZE];
      }
    }
  }

  auto result = mesh::Mesh::onRecvPacket(pkt);

  // restore identity
  if (swapped) {
    self_id = original_id;
  }
  return result;
}

void MyMesh::loop() {
  mesh::Mesh::loop();
  unsigned long now = millis();

  if (next_local_advert && (long)(now - next_local_advert) >= 0) {
    updateAdvertTimer();
    sendSelfAdvertisement(200, false);
  }

  if (next_room_advert && (long)(now - next_room_advert) >= 0) {
    sendRoomAdvertisement(0);
    next_room_advert = 0;
  }

  if (next_flood_advert && now >= next_flood_advert) {
    next_flood_advert =
        futureMillis((uint32_t)_prefs.flood_advert_interval * 60 * 60 * 1000);
    sendSelfAdvertisement(2000, true);
  }

  if (millisHasNowPassed(next_push) && acl.getNumClients() > 0) {
    for (int i = 0; i < acl.getNumClients(); i++) {
      auto c = acl.getClientByIdx(i);
      if (c->extra.room.pending_ack &&
          millisHasNowPassed(c->extra.room.ack_timeout)) {
        c->extra.room.push_failures++;
        c->extra.room.pending_ack = 0;
      }
    }
    auto client = acl.getClientByIdx(next_client_idx);
    bool did_push = false;
    if (client->extra.room.pending_ack == 0 && client->last_activity != 0 &&
        client->extra.room.push_failures < 3) {
      uint32_t now = getRTCClock()->getCurrentTime();
      for (int k = 0, idx = next_post_idx; k < MAX_UNSYNCED_POSTS; k++) {
        auto p = &posts[idx];
        if (p->post_timestamp == 0) {
          idx = (idx + 1) % MAX_UNSYNCED_POSTS;
          continue;
        }

        bool time_check = now >= p->post_timestamp + POST_SYNC_DELAY_SECS;
        bool sync_check = p->post_timestamp > client->extra.room.sync_since;
        bool author_check = !p->author.matches(client->id);

        if (time_check && sync_check && author_check) {
          // push this post to client, then wait for ACK
          pushPostToClient(client, *p);
          did_push = true;
          break;
        }
        idx = (idx + 1) % MAX_UNSYNCED_POSTS;
      }
    } else {
      // skipping busy or evicted client
    }
    next_client_idx = (next_client_idx + 1) % acl.getNumClients();

    if (did_push) {
      next_push = futureMillis(SYNC_PUSH_INTERVAL);
    } else {
      next_push = futureMillis(SYNC_PUSH_INTERVAL / 8);
    }
  }

  if (now >= last_millis + 1000) {
    last_millis = now;
    uptime_millis += 1000;
  }

#ifdef WITH_BRIDGE
  bridge.update();
#endif
}

void MyMesh::applyTempRadioParams(float freq, float bw, uint8_t sf, uint8_t cr,
                                  int timeout_mins) {
  set_radio_at = futureMillis(2000);
  pending_freq = freq;
  pending_bw = bw;
  pending_sf = sf;
  pending_cr = cr;
  revert_radio_at = futureMillis(2000 + timeout_mins * 60 * 1000);
}

bool MyMesh::formatFileSystem() {
#if defined(NRF52_PLATFORM) || defined(STM32_PLATFORM)
  return InternalFS.format();
#elif defined(RP2040_PLATFORM)
  return LittleFS.format();
#elif defined(ESP32)
  return SPIFFS.format();
#else
  return false;
#endif
}

void MyMesh::updateAdvertTimer() {
  if (_prefs.advert_interval > 0) {
    next_local_advert =
        futureMillis((uint32_t)_prefs.advert_interval * 2 * 60 * 1000);
  } else {
    next_local_advert = 0;
  }
}
void MyMesh::updateFloodAdvertTimer() {
  if (_prefs.flood_advert_interval > 0) {
    next_flood_advert =
        futureMillis(((uint32_t)_prefs.flood_advert_interval) * 60 * 60 * 1000);
  } else {
    next_flood_advert = 0;
  }
}

void MyMesh::dumpLogFile() {
#if defined(RP2040_PLATFORM)
  File f = _fs->open(PACKET_LOG_FILE, "r");
#else
  File f = _fs->open(PACKET_LOG_FILE);
#endif
  if (f) {
    while (f.available()) {
      int c = f.read();
      if (c < 0) break;
      Serial.print((char)c);
    }
    f.close();
  }
}

void MyMesh::setTxPower(int8_t power_dbm) { radio_set_tx_power(power_dbm); }

void MyMesh::saveIdentity(const mesh::LocalIdentity& new_id) {
  self_id = new_id;
  IdentityStore store(*_fs, "/identity");
#if defined(NRF52_PLATFORM) || defined(STM32_PLATFORM)
  // identityStore handles path translation usually?
#endif
  store.save("_main", self_id);
}

void MyMesh::clearStats() {
  radio_driver.resetStats();
  resetStats();
  ((SimpleMeshTables*)getTables())->resetStats();
}

void MyMesh::formatStatsReply(char* reply) {
  StatsFormatHelper::formatCoreStats(reply, board, *_ms, _err_flags, _mgr);
}

void MyMesh::formatRadioStatsReply(char* reply) {
  StatsFormatHelper::formatRadioStats(reply, _radio, radio_driver,
                                      getTotalAirTime(), getReceiveAirTime());
}

void MyMesh::formatPacketStatsReply(char* reply) {
  StatsFormatHelper::formatPacketStats(reply, radio_driver, getNumSentFlood(),
                                       getNumSentDirect(), getNumRecvFlood(),
                                       getNumRecvDirect());
}

void MyMesh::handleCommand(uint32_t sender_timestamp, char* command,
                           char* reply) {
  if (region_load_active) {
    if (StrHelper::isBlank(command)) {  // empty/blank line, signal to terminate
                                        // 'load' operation
      region_map = temp_map;  // copy over the temp instance as new current map
      region_load_active = false;

      sprintf(reply, "OK - loaded %d regions", region_map.getCount());
    } else {
      char* np = command;
      while (*np == ' ') np++;  // skip indent
      int indent = np - command;

      char* ep = np;
      while (RegionMap::is_name_char(*ep)) ep++;
      if (*ep) {
        *ep++ = 0;
      }  // set null terminator for end of name

      while (*ep && *ep != 'F') ep++;  // look for (optional) flags

      if (indent > 0 && indent < 8 && strlen(np) > 0) {
        auto parent = load_stack[indent - 1];
        if (parent) {
          auto old = region_map.findByName(np);
          auto nw = temp_map.putRegion(
              np, parent->id,
              old ? old->id
                  : 0);  // carry-over the current ID (if name already exists)
          if (nw) {
            nw->flags =
                old ? old->flags
                    : (*ep == 'F'
                           ? 0
                           : REGION_DENY_FLOOD);  // carry-over flags from curr

            load_stack[indent] =
                nw;  // keep pointers to parent regions, to resolve parent_id's
          }
        }
      }
      reply[0] = 0;
    }
    return;
  }

  while (*command == ' ') command++;  // skip leading spaces

  if (strlen(command) > 4 &&
      command[2] == '|') {      // optional prefix (for companion radio CLI)
    memcpy(reply, command, 3);  // reflect the prefix back
    reply += 3;
    command += 3;
  }

  // handle ACL related commands
  if (memcmp(command, "setperm ", 8) ==
      0) {  // format:  setperm {pubkey-hex} {permissions-int8}
    char* hex = &command[8];
    char* sp = strchr(hex, ' ');  // look for separator char
    if (sp == NULL) {
      strcpy(reply, "Err - bad params");
    } else {
      *sp++ = 0;  // replace space with null terminator

      uint8_t pubkey[PUB_KEY_SIZE];
      int hex_len = min(sp - hex, PUB_KEY_SIZE * 2);
      if (mesh::Utils::fromHex(pubkey, hex_len / 2, hex)) {
        uint8_t perms = atoi(sp);
        if (acl.applyPermissions(self_id, pubkey, hex_len / 2, perms)) {
          dirty_contacts_expiry =
              futureMillis(LAZY_CONTACTS_WRITE_DELAY);  // trigger acl.save()
          strcpy(reply, "OK");
        } else {
          strcpy(reply, "Err - invalid params");
        }
      } else {
        strcpy(reply, "Err - bad pubkey");
      }
    }
  } else if (sender_timestamp == 0 && strcmp(command, "get acl") == 0) {
    Serial.println("ACL:");
    for (int i = 0; i < acl.getNumClients(); i++) {
      auto c = acl.getClientByIdx(i);
      if (c->permissions == 0) continue;  // skip deleted (or guest) entries

      Serial.printf("%02X ", c->permissions);
      mesh::Utils::printHex(Serial, c->id.pub_key, PUB_KEY_SIZE);
      Serial.printf("\n");
    }
    reply[0] = 0;
  } else if (memcmp(command, "region", 6) == 0) {
    reply[0] = 0;

    const char* parts[4];
    int n = mesh::Utils::parseTextParts(command, parts, 4, ' ');
    if (n == 1) {
      region_map.exportTo(reply, 160);
    } else if (n >= 2 && strcmp(parts[1], "load") == 0) {
      temp_map.resetFrom(region_map);  // rebuild regions in a temp instance
      memset(load_stack, 0, sizeof(load_stack));
      load_stack[0] = &temp_map.getWildcard();
      region_load_active = true;
    } else if (n >= 2 && strcmp(parts[1], "save") == 0) {
      _prefs.discovery_mod_timestamp =
          rtc_clock.getCurrentTime();  // this node is now 'modified' (for
                                       // discovery info)
      savePrefs();
      bool success = region_map.save(_fs);
      strcpy(reply, success ? "OK" : "Err - save failed");
    } else if (n >= 3 && strcmp(parts[1], "allowf") == 0) {
      auto region = region_map.findByNamePrefix(parts[2]);
      if (region) {
        region->flags &= ~REGION_DENY_FLOOD;
        strcpy(reply, "OK");
      } else {
        strcpy(reply, "Err - unknown region");
      }
    } else if (n >= 3 && strcmp(parts[1], "denyf") == 0) {
      auto region = region_map.findByNamePrefix(parts[2]);
      if (region) {
        region->flags |= REGION_DENY_FLOOD;
        strcpy(reply, "OK");
      } else {
        strcpy(reply, "Err - unknown region");
      }
    } else if (n >= 3 && strcmp(parts[1], "get") == 0) {
      auto region = region_map.findByNamePrefix(parts[2]);
      if (region) {
        auto parent = region_map.findById(region->parent);
        if (parent && parent->id != 0) {
          sprintf(reply, " %s (%s) %s", region->name, parent->name,
                  (region->flags & REGION_DENY_FLOOD) ? "" : "F");
        } else {
          sprintf(reply, " %s %s", region->name,
                  (region->flags & REGION_DENY_FLOOD) ? "" : "F");
        }
      } else {
        strcpy(reply, "Err - unknown region");
      }
    } else if (n >= 3 && strcmp(parts[1], "home") == 0) {
      auto home = region_map.findByNamePrefix(parts[2]);
      if (home) {
        region_map.setHomeRegion(home);
        sprintf(reply, " home is now %s", home->name);
      } else {
        strcpy(reply, "Err - unknown region");
      }
    } else if (n == 2 && strcmp(parts[1], "home") == 0) {
      auto home = region_map.getHomeRegion();
      sprintf(reply, " home is %s", home ? home->name : "*");
    } else if (n >= 3 && strcmp(parts[1], "put") == 0) {
      auto parent = n >= 4 ? region_map.findByNamePrefix(parts[3])
                           : &region_map.getWildcard();
      if (parent == NULL) {
        strcpy(reply, "Err - unknown parent");
      } else {
        auto region = region_map.putRegion(parts[2], parent->id);
        if (region == NULL) {
          strcpy(reply, "Err - unable to put");
        } else {
          strcpy(reply, "OK");
        }
      }
    } else if (n >= 3 && strcmp(parts[1], "remove") == 0) {
      auto region = region_map.findByName(parts[2]);
      if (region) {
        if (region_map.removeRegion(*region)) {
          strcpy(reply, "OK");
        } else {
          strcpy(reply, "Err - not empty");
        }
      } else {
        strcpy(reply, "Err - not found");
      }
    } else if (n >= 3 && strcmp(parts[1], "list") == 0) {
      uint8_t mask = 0;
      bool invert = false;

      if (strcmp(parts[2], "allowed") == 0) {
        mask = REGION_DENY_FLOOD;
        invert = false;  // list regions that DON'T have DENY flag
      } else if (strcmp(parts[2], "denied") == 0) {
        mask = REGION_DENY_FLOOD;
        invert = true;  // list regions that DO have DENY flag
      } else {
        strcpy(reply, "Err - use 'allowed' or 'denied'");
        return;
      }

      int len = region_map.exportNamesTo(reply, 160, mask, invert);
      if (len == 0) {
        strcpy(reply, "-none-");
      }
    } else {
      strcpy(reply, "Err - ??");
    }
  } else {
    _cli.handleCommand(sender_timestamp, command,
                       reply);  // common CLI commands
  }
}

void MyMesh::removeNeighbor(const uint8_t* pubkey, int key_len) {
#if MAX_NEIGHBOURS
  for (int i = 0; i < MAX_NEIGHBOURS; i++) {
    NeighbourInfo* neighbour = &neighbours[i];
    if (memcmp(neighbour->id.pub_key, pubkey, key_len) == 0) {
      neighbours[i] = NeighbourInfo();  // clear neighbour entry
    }
  }
#endif
}

void MyMesh::formatNeighborsReply(char* reply) {
  strcpy(reply, "Use 'get neighbours'");
}

// To check if there is pending work
bool MyMesh::hasPendingWork() const {
  return _mgr->getOutboundCount(0xFFFFFFFF) > 0;
}
