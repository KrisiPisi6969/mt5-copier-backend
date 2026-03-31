#property strict
#property version   "2.00"
#property description "MT5 Copier Web + Panel + Symbol Mapping"

#include <Trade/Trade.mqh>

CTrade trade;

enum CopierRole
{
   ROLE_MASTER = 0,
   ROLE_SLAVE  = 1
};

struct RemotePosition
{
   string symbol;
   string type;
   double volume;
   double sl;
   double tp;
};

struct DesiredSymbolPosition
{
   string symbol;
   double signed_volume;
   double sl;
   double tp;
};

//====================================================
// Inputs
//====================================================
input CopierRole InpRole               = ROLE_MASTER;
input string     InpApiBase            = "https://mt5-copier-test.onrender.com";
input string     InpMasterToken        = "MASTER123";
input string     InpSlaveLicenseKey    = "TEST-001";

input bool       InpUseTimer           = true;
input int        InpTimerMs            = 200;

input double     InpLotMultiplier      = 1.0;
input bool       InpUseFixedLot        = false;
input double     InpFixedLot           = 0.10;

input long       InpMagic              = 770001;
input int        InpMaxDeviationPoints = 20;
input bool       InpCopySLTP           = true;

input string     InpAllowSymbolsCsv    = "";
input string     InpDenySymbolsCsv     = "";
input string     InpSymbolMapCsv       = "";
input bool       InpUseDirectMatch     = true;

input string     InpCommentPrefix      = "COPIER";

//====================================================
// Runtime State
//====================================================
CopierRole g_role                   = ROLE_MASTER;
string     g_api_base               = "";
string     g_master_token           = "";
string     g_slave_license_key      = "";
bool       g_use_timer              = true;
int        g_timer_ms               = 200;
double     g_lot_multiplier         = 1.0;
bool       g_use_fixed_lot          = false;
double     g_fixed_lot              = 0.10;
long       g_magic                  = 770001;
int        g_max_deviation_points   = 20;
bool       g_copy_sltp              = true;
string     g_allow_symbols_csv      = "";
string     g_deny_symbols_csv       = "";
string     g_symbol_map_csv         = "";
bool       g_use_direct_match       = true;
string     g_comment_prefix         = "COPIER";
bool       g_enabled                = true;

string     g_last_snapshot_id       = "";
bool       g_is_hedge_account       = false;
bool       g_slave_activated        = false;
datetime   g_last_publish_time      = 0;
datetime   g_last_sync_time         = 0;
long       g_chart_id               = 0;

//====================================================
// UI
//====================================================
string UI = "RCXW_";
bool   g_panel_expanded = true;

int PANEL_X = 18;
int PANEL_Y = 72;
int PANEL_W = 610;
int PANEL_H = 560;
int PANEL_W_MIN = 430;
int PANEL_H_MIN = 42;

//====================================================
// Helpers
//====================================================
string Trim(const string s)
{
   string x = s;
   StringTrimLeft(x);
   StringTrimRight(x);
   return x;
}

string ToUpperCopy(string s)
{
   StringToUpper(s);
   return s;
}

string BoolText(bool v) { return v ? "ON" : "OFF"; }

string RoleText(CopierRole r)
{
   return (r == ROLE_MASTER ? "MASTER" : "SLAVE");
}

string TimeText(datetime t)
{
   if(t <= 0) return "-";
   return TimeToString(t, TIME_DATE | TIME_SECONDS);
}

string AccountModeText()
{
   return (g_is_hedge_account ? "HEDGING" : "NETTING");
}

bool CsvContains(const string csv, const string value)
{
   string text = Trim(csv);
   if(text == "")
      return false;

   string parts[];
   int n = StringSplit(text, ',', parts);
   string v = ToUpperCopy(Trim(value));

   for(int i = 0; i < n; i++)
   {
      if(ToUpperCopy(Trim(parts[i])) == v)
         return true;
   }
   return false;
}

bool SymbolAllowed(const string symbol)
{
   if(Trim(g_allow_symbols_csv) != "" && !CsvContains(g_allow_symbols_csv, symbol))
      return false;
   if(Trim(g_deny_symbols_csv) != "" && CsvContains(g_deny_symbols_csv, symbol))
      return false;
   return true;
}

bool SymbolExistsLocal(const string symbol)
{
   string s = Trim(symbol);
   if(s == "")
      return false;

   ResetLastError();
   if(SymbolSelect(s, true))
      return true;

   return false;
}

string ResolveMappedSlaveSymbolOnly(const string master_symbol)
{
   string src = Trim(master_symbol);
   if(src == "")
      return "";

   string text = Trim(g_symbol_map_csv);
   if(text == "")
      return "";

   string pairs[];
   int n = StringSplit(text, ',', pairs);
   string src_upper = ToUpperCopy(src);

   for(int i = 0; i < n; i++)
   {
      string pair = Trim(pairs[i]);
      if(pair == "")
         continue;

      int eq = StringFind(pair, "=");
      if(eq < 0)
         continue;

      string left  = Trim(StringSubstr(pair, 0, eq));
      string right = Trim(StringSubstr(pair, eq + 1));

      if(ToUpperCopy(left) != src_upper)
         continue;

      if(SymbolExistsLocal(right))
         return right;

      Print("Symbol mapping exists but local symbol missing. master=", src, " mapped=", right);
      return "";
   }

   return "";
}

string ResolveSlaveSymbol(const string master_symbol)
{
   string src = Trim(master_symbol);
   if(src == "")
      return "";

   string mapped = ResolveMappedSlaveSymbolOnly(src);
   if(mapped != "")
      return mapped;

   if(g_use_direct_match && SymbolExistsLocal(src))
      return src;

   return "";
}

void DetectAccountMode()
{
   ENUM_ACCOUNT_MARGIN_MODE mm = (ENUM_ACCOUNT_MARGIN_MODE)AccountInfoInteger(ACCOUNT_MARGIN_MODE);
   g_is_hedge_account = (mm == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING);
}

string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   StringReplace(s, "\r", " ");
   StringReplace(s, "\n", " ");
   return s;
}

string PositionTypeText(long position_type)
{
   if(position_type == POSITION_TYPE_BUY)
      return "BUY";
   return "SELL";
}

double SymbolVolumeMin(const string symbol)
{
   double v = 0.0;
   SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN, v);
   return v;
}

double SymbolVolumeMax(const string symbol)
{
   double v = 0.0;
   SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX, v);
   return v;
}

double SymbolVolumeStep(const string symbol)
{
   double v = 0.0;
   SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP, v);
   return v;
}

double NormalizeVolumeForSymbol(const string symbol, double vol)
{
   double step = SymbolVolumeStep(symbol);
   double vmin = SymbolVolumeMin(symbol);
   double vmax = SymbolVolumeMax(symbol);

   if(step <= 0.0) step = 0.01;

   vol = MathFloor(vol / step + 1e-8) * step;
   vol = NormalizeDouble(vol, 8);

   if(vol > 0.0 && vol < vmin)
      vol = vmin;
   if(vmax > 0.0 && vol > vmax)
      vol = vmax;
   if(vol < 0.0)
      vol = 0.0;

   return vol;
}

double ApplyLotModel(const string symbol, double master_volume)
{
   double result = (g_use_fixed_lot ? g_fixed_lot : master_volume * g_lot_multiplier);
   return NormalizeVolumeForSymbol(symbol, result);
}

bool HttpPostJson(const string url, const string json, string &response)
{
   char data[];
   char result[];
   string result_headers = "";

   StringToCharArray(json, data, 0, StringLen(json), CP_UTF8);

   string headers = "Content-Type: application/json\r\n";

   ResetLastError();
   int status = WebRequest("POST", url, headers, 5000, data, result, result_headers);

   if(status == -1)
   {
      Print("WebRequest failed. url=", url, " err=", GetLastError());
      return false;
   }

   response = CharArrayToString(result, 0, -1, CP_UTF8);

   Print("HTTP status=", status, " url=", url);
   Print("HTTP response=", response);

   return (status >= 200 && status < 300);
}

//====================================================
// Build JSON - MASTER
//====================================================
string BuildSnapshotJson()
{
   string snapshot_id = IntegerToString((long)TimeLocal()) + "_" + IntegerToString((int)GetTickCount());

   string json = "{";
   json += "\"master_token\":\"" + JsonEscape(g_master_token) + "\",";
   json += "\"snapshot_id\":\"" + snapshot_id + "\",";
   json += "\"positions\":[";

   bool first = true;

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      if(!SymbolAllowed(symbol))
         continue;

      long   type   = PositionGetInteger(POSITION_TYPE);
      double volume = PositionGetDouble(POSITION_VOLUME);
      double sl     = PositionGetDouble(POSITION_SL);
      double tp     = PositionGetDouble(POSITION_TP);

      if(!first)
         json += ",";

      json += "{";
      json += "\"symbol\":\"" + JsonEscape(symbol) + "\",";
      json += "\"type\":\"" + PositionTypeText(type) + "\",";
      json += "\"volume\":" + DoubleToString(volume, 2) + ",";
      json += "\"sl\":" + DoubleToString(sl, 8) + ",";
      json += "\"tp\":" + DoubleToString(tp, 8);
      json += "}";

      first = false;
   }

   json += "],";
   json += "\"pending_orders\":[]";
   json += "}";

   return json;
}

//====================================================
// Simple JSON parse helpers
//====================================================
bool JsonGetString(const string json, const string key, string &value)
{
   value = "";
   string pattern = "\"" + key + "\":\"";
   int p = StringFind(json, pattern);
   if(p < 0)
      return false;

   int start = p + StringLen(pattern);
   int end = start;

   while(end < StringLen(json))
   {
      ushort c = (ushort)StringGetCharacter(json, end);
      if(c == '\"')
         break;
      end++;
   }

   value = StringSubstr(json, start, end - start);
   return true;
}

bool JsonGetNumber(const string json, const string key, double &value)
{
   value = 0.0;
   string pattern = "\"" + key + "\":";
   int p = StringFind(json, pattern);
   if(p < 0)
      return false;

   int start = p + StringLen(pattern);
   int end = start;

   while(end < StringLen(json))
   {
      ushort c = (ushort)StringGetCharacter(json, end);
      bool ok = ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.');
      if(!ok)
         break;
      end++;
   }

   string num = StringSubstr(json, start, end - start);
   value = StringToDouble(num);
   return true;
}

bool JsonGetBool(const string json, const string key, bool &value)
{
   value = false;
   string patternTrue  = "\"" + key + "\":true";
   string patternFalse = "\"" + key + "\":false";

   if(StringFind(json, patternTrue) >= 0)
   {
      value = true;
      return true;
   }

   if(StringFind(json, patternFalse) >= 0)
   {
      value = false;
      return true;
   }

   return false;
}

bool JsonGetArrayText(const string json, const string key, string &array_text)
{
   array_text = "";
   string pattern = "\"" + key + "\":[";
   int p = StringFind(json, pattern);
   if(p < 0)
      return false;

   int start = p + StringLen(pattern) - 1;
   int depth = 0;
   int end = -1;

   for(int i = start; i < StringLen(json); i++)
   {
      ushort c = (ushort)StringGetCharacter(json, i);

      if(c == '[')
         depth++;
      else if(c == ']')
      {
         depth--;
         if(depth == 0)
         {
            end = i;
            break;
         }
      }
   }

   if(end < 0)
      return false;

   array_text = StringSubstr(json, start, end - start + 1);
   return true;
}

int SplitTopLevelObjects(const string array_text, string &objects[])
{
   ArrayResize(objects, 0);

   int depth = 0;
   int obj_start = -1;

   for(int i = 0; i < StringLen(array_text); i++)
   {
      ushort c = (ushort)StringGetCharacter(array_text, i);

      if(c == '{')
      {
         if(depth == 0)
            obj_start = i;
         depth++;
      }
      else if(c == '}')
      {
         depth--;
         if(depth == 0 && obj_start >= 0)
         {
            int idx = ArraySize(objects);
            ArrayResize(objects, idx + 1);
            objects[idx] = StringSubstr(array_text, obj_start, i - obj_start + 1);
            obj_start = -1;
         }
      }
   }

   return ArraySize(objects);
}

bool ParseRemotePositionObject(const string obj, RemotePosition &p)
{
   p.symbol = "";
   p.type   = "";
   p.volume = 0.0;
   p.sl     = 0.0;
   p.tp     = 0.0;

   if(!JsonGetString(obj, "symbol", p.symbol))
      return false;
   if(!JsonGetString(obj, "type", p.type))
      return false;

   JsonGetNumber(obj, "volume", p.volume);
   JsonGetNumber(obj, "sl", p.sl);
   JsonGetNumber(obj, "tp", p.tp);

   return true;
}

bool ParsePositionsFromPullResponse(const string json, string &snapshot_id, RemotePosition &positions[])
{
   ArrayResize(positions, 0);
   snapshot_id = "";

   bool has_update = false;
   if(!JsonGetBool(json, "has_update", has_update))
      return false;

   JsonGetString(json, "snapshot_id", snapshot_id);

   if(!has_update)
      return true;

   string arr_text;
   if(!JsonGetArrayText(json, "positions", arr_text))
      return false;

   string objs[];
   int n = SplitTopLevelObjects(arr_text, objs);

   for(int i = 0; i < n; i++)
   {
      RemotePosition rp;
      if(!ParseRemotePositionObject(objs[i], rp))
         continue;

      int idx = ArraySize(positions);
      ArrayResize(positions, idx + 1);
      positions[idx] = rp;
   }

   return true;
}

//====================================================
// Activate / Pull
//====================================================
string BuildActivateJson()
{
   long login = (long)AccountInfoInteger(ACCOUNT_LOGIN);
   string server = AccountInfoString(ACCOUNT_SERVER);
   string machine = TerminalInfoString(TERMINAL_PATH);

   string json = "{";
   json += "\"license_key\":\"" + JsonEscape(g_slave_license_key) + "\",";
   json += "\"account_login\":\"" + IntegerToString((int)login) + "\",";
   json += "\"broker_server\":\"" + JsonEscape(server) + "\",";
   json += "\"machine_id\":\"" + JsonEscape(machine) + "\"";
   json += "}";

   return json;
}

bool ActivateSlaveOnline()
{
   string url = g_api_base + "/slave/activate";
   string json = BuildActivateJson();
   string response;

   bool ok = HttpPostJson(url, json, response);
   if(!ok)
      return false;

   if(StringFind(response, "\"ok\":true") >= 0)
   {
      g_slave_activated = true;
      Print("Slave activation OK.");
      return true;
   }

   Print("Slave activation FAILED.");
   return false;
}

string BuildPullJson()
{
   string json = "{";
   json += "\"license_key\":\"" + JsonEscape(g_slave_license_key) + "\",";
   json += "\"last_snapshot_id\":\"" + JsonEscape(g_last_snapshot_id) + "\"";
   json += "}";

   return json;
}

//====================================================
// Sync logic
//====================================================
int FindDesiredBySymbol(DesiredSymbolPosition &arr[], const string symbol)
{
   for(int i = 0; i < ArraySize(arr); i++)
      if(arr[i].symbol == symbol)
         return i;
   return -1;
}

void AggregateDesiredPositions(RemotePosition &remote_positions[], DesiredSymbolPosition &desired[])
{
   ArrayResize(desired, 0);

   for(int i = 0; i < ArraySize(remote_positions); i++)
   {
      string slave_symbol = ResolveSlaveSymbol(remote_positions[i].symbol);
      if(slave_symbol == "")
      {
         Print("No mapping/direct symbol found for master symbol: ", remote_positions[i].symbol);
         continue;
      }

      if(!SymbolAllowed(slave_symbol))
         continue;

      int idx = FindDesiredBySymbol(desired, slave_symbol);
      double signed_vol = remote_positions[i].volume;
      if(ToUpperCopy(remote_positions[i].type) == "SELL")
         signed_vol = -signed_vol;

      if(idx < 0)
      {
         DesiredSymbolPosition d;
         d.symbol = slave_symbol;
         d.signed_volume = signed_vol;
         d.sl = remote_positions[i].sl;
         d.tp = remote_positions[i].tp;

         int n = ArraySize(desired);
         ArrayResize(desired, n + 1);
         desired[n] = d;
      }
      else
      {
         desired[idx].signed_volume += signed_vol;

         if(remote_positions[i].sl != 0.0)
            desired[idx].sl = remote_positions[i].sl;
         if(remote_positions[i].tp != 0.0)
            desired[idx].tp = remote_positions[i].tp;
      }
   }
}

bool GetNettingPositionState(const string symbol, double &signed_volume, double &sl, double &tp)
{
   signed_volume = 0.0;
   sl = 0.0;
   tp = 0.0;

   if(!PositionSelect(symbol))
      return false;

   long type = PositionGetInteger(POSITION_TYPE);
   double vol = PositionGetDouble(POSITION_VOLUME);
   sl = PositionGetDouble(POSITION_SL);
   tp = PositionGetDouble(POSITION_TP);

   signed_volume = (type == POSITION_TYPE_BUY ? vol : -vol);
   return true;
}

bool SendMarketDelta(const string symbol, double signed_delta)
{
   double volume = NormalizeVolumeForSymbol(symbol, MathAbs(signed_delta));
   if(volume <= 0.0)
      return true;

   trade.SetExpertMagicNumber(g_magic);
   trade.SetDeviationInPoints(g_max_deviation_points);

   string comment = g_comment_prefix + "|WEB";

   bool ok = false;
   if(signed_delta > 0.0)
      ok = trade.Buy(volume, symbol, 0.0, 0.0, 0.0, comment);
   else
      ok = trade.Sell(volume, symbol, 0.0, 0.0, 0.0, comment);

   if(!ok)
   {
      Print("SendMarketDelta failed. symbol=", symbol,
            " delta=", DoubleToString(signed_delta, 8),
            " retcode=", trade.ResultRetcode(),
            " desc=", trade.ResultRetcodeDescription());
   }

   return ok;
}

bool CloseManagedPositionByTicket(ulong ticket)
{
   if(ticket == 0)
      return true;

   if(!PositionSelectByTicket(ticket))
      return true;

   trade.SetExpertMagicNumber(g_magic);
   trade.SetDeviationInPoints(g_max_deviation_points);

   bool ok = trade.PositionClose(ticket, g_max_deviation_points);
   if(!ok)
   {
      Print("PositionClose(ticket) failed. ticket=", (long)ticket,
            " retcode=", trade.ResultRetcode(),
            " desc=", trade.ResultRetcodeDescription());
   }

   return ok;
}

bool OpenManagedPosition(const string symbol, double signed_volume, double sl, double tp)
{
   double vol = NormalizeVolumeForSymbol(symbol, MathAbs(signed_volume));
   if(vol <= 0.0)
      return true;

   trade.SetExpertMagicNumber(g_magic);
   trade.SetDeviationInPoints(g_max_deviation_points);

   string comment = g_comment_prefix + "|WEB";

   bool ok = false;
   if(signed_volume > 0.0)
      ok = trade.Buy(vol, symbol, 0.0, sl, tp, comment);
   else
      ok = trade.Sell(vol, symbol, 0.0, sl, tp, comment);

   if(!ok)
   {
      Print("OpenManagedPosition failed. symbol=", symbol,
            " retcode=", trade.ResultRetcode(),
            " desc=", trade.ResultRetcodeDescription());
   }

   return ok;
}

void SyncNettingPositions(DesiredSymbolPosition &desired[])
{
   for(int i = 0; i < ArraySize(desired); i++)
   {
      string symbol = desired[i].symbol;
      SymbolSelect(symbol, true);

      double desired_abs = ApplyLotModel(symbol, MathAbs(desired[i].signed_volume));
      double desired_signed = (desired[i].signed_volume >= 0.0 ? desired_abs : -desired_abs);

      double current_signed = 0.0, current_sl = 0.0, current_tp = 0.0;
      bool has_pos = GetNettingPositionState(symbol, current_signed, current_sl, current_tp);
      if(!has_pos)
         current_signed = 0.0;

      double delta = NormalizeDouble(desired_signed - current_signed, 8);
      double step = SymbolVolumeStep(symbol);
      if(step <= 0.0) step = 0.01;

      if(MathAbs(delta) >= step * 0.5)
         SendMarketDelta(symbol, delta);

      if(g_copy_sltp)
      {
         Sleep(60);
         if(PositionSelect(symbol))
         {
            trade.SetExpertMagicNumber(g_magic);
            trade.SetDeviationInPoints(g_max_deviation_points);
            if(!trade.PositionModify(symbol, desired[i].sl, desired[i].tp))
            {
               Print("PositionModify failed for ", symbol,
                     " retcode=", trade.ResultRetcode(),
                     " desc=", trade.ResultRetcodeDescription());
            }
         }
      }
   }

   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(magic != g_magic)
         continue;

      string symbol = PositionGetString(POSITION_SYMBOL);
      if(FindDesiredBySymbol(desired, symbol) < 0)
         CloseManagedPositionByTicket(ticket);
   }
}

void SyncHedgingPositions(DesiredSymbolPosition &desired[])
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket))
         continue;

      long magic = PositionGetInteger(POSITION_MAGIC);
      if(magic != g_magic)
         continue;

      CloseManagedPositionByTicket(ticket);
   }

   Sleep(120);

   for(int i = 0; i < ArraySize(desired); i++)
   {
      string symbol = desired[i].symbol;
      SymbolSelect(symbol, true);

      double desired_abs = ApplyLotModel(symbol, MathAbs(desired[i].signed_volume));
      double desired_signed = (desired[i].signed_volume >= 0.0 ? desired_abs : -desired_abs);

      OpenManagedPosition(symbol, desired_signed,
                          (g_copy_sltp ? desired[i].sl : 0.0),
                          (g_copy_sltp ? desired[i].tp : 0.0));
   }
}

void SyncRemotePositions(RemotePosition &remote_positions[])
{
   DesiredSymbolPosition desired[];
   AggregateDesiredPositions(remote_positions, desired);

   if(g_is_hedge_account)
      SyncHedgingPositions(desired);
   else
      SyncNettingPositions(desired);
}

//====================================================
// Actions
//====================================================
void PublishSnapshot()
{
   if(g_role != ROLE_MASTER) return;
   if(!g_enabled) return;

   string json = BuildSnapshotJson();
   string response;
   string url = g_api_base + "/master/publish";

   bool ok = HttpPostJson(url, json, response);
   if(ok)
   {
      g_last_publish_time = TimeLocal();
      Print("Master publish OK.");
   }
   else
   {
      Print("Master publish FAILED.");
   }
}

void SlaveSync()
{
   if(g_role != ROLE_SLAVE) return;
   if(!g_enabled) return;

   if(!g_slave_activated)
   {
      if(!ActivateSlaveOnline())
         return;
   }

   string json = BuildPullJson();
   string response;
   string url = g_api_base + "/slave/pull";

   bool ok = HttpPostJson(url, json, response);
   if(!ok)
   {
      Print("Slave pull FAILED.");
      return;
   }

   if(StringFind(response, "\"ok\":true") < 0)
   {
      Print("Slave pull invalid response.");
      return;
   }

   string snapshot_id = "";
   RemotePosition remote_positions[];

   if(!ParsePositionsFromPullResponse(response, snapshot_id, remote_positions))
   {
      Print("Failed to parse pull response.");
      return;
   }

   bool has_update = false;
   JsonGetBool(response, "has_update", has_update);

   if(!has_update)
      return;

   if(snapshot_id == g_last_snapshot_id)
      return;

   g_last_snapshot_id = snapshot_id;
   SyncRemotePositions(remote_positions);
   g_last_sync_time = TimeLocal();

   Print("Slave sync OK. snapshot_id=", g_last_snapshot_id,
         " positions=", ArraySize(remote_positions));
}

//====================================================
// UI primitives
//====================================================
void DeleteIfExists(const string name)
{
   if(ObjectFind(g_chart_id, name) >= 0)
      ObjectDelete(g_chart_id, name);
}

bool CreateRect(const string name, int x, int y, int w, int h, color bg, color border)
{
   if(ObjectFind(g_chart_id, name) < 0)
      if(!ObjectCreate(g_chart_id, name, OBJ_RECTANGLE_LABEL, 0, 0, 0))
         return false;

   ObjectSetInteger(g_chart_id, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XSIZE, w);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YSIZE, h);
   ObjectSetInteger(g_chart_id, name, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(g_chart_id, name, OBJPROP_COLOR, border);
   ObjectSetInteger(g_chart_id, name, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(g_chart_id, name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(g_chart_id, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(g_chart_id, name, OBJPROP_SELECTED, false);
   return true;
}

bool CreateLabel(const string name, int x, int y, const string txt, int fs, color clr, bool bold=false)
{
   if(ObjectFind(g_chart_id, name) < 0)
      if(!ObjectCreate(g_chart_id, name, OBJ_LABEL, 0, 0, 0))
         return false;

   ObjectSetInteger(g_chart_id, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(g_chart_id, name, OBJPROP_FONTSIZE, fs);
   ObjectSetInteger(g_chart_id, name, OBJPROP_COLOR, clr);
   ObjectSetString(g_chart_id, name, OBJPROP_FONT, bold ? "Arial Bold" : "Arial");
   ObjectSetString(g_chart_id, name, OBJPROP_TEXT, txt);
   ObjectSetInteger(g_chart_id, name, OBJPROP_HIDDEN, true);
   ObjectSetInteger(g_chart_id, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(g_chart_id, name, OBJPROP_SELECTED, false);
   return true;
}

bool CreateButton(const string name, int x, int y, int w, int h, const string txt, color bg, color fg)
{
   if(ObjectFind(g_chart_id, name) < 0)
      if(!ObjectCreate(g_chart_id, name, OBJ_BUTTON, 0, 0, 0))
         return false;

   ObjectSetInteger(g_chart_id, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XSIZE, w);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YSIZE, h);
   ObjectSetInteger(g_chart_id, name, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(g_chart_id, name, OBJPROP_COLOR, fg);
   ObjectSetInteger(g_chart_id, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(g_chart_id, name, OBJPROP_FONT, "Arial");
   ObjectSetString(g_chart_id, name, OBJPROP_TEXT, txt);
   ObjectSetInteger(g_chart_id, name, OBJPROP_HIDDEN, true);
   return true;
}

bool CreateEdit(const string name, int x, int y, int w, int h, const string txt)
{
   if(ObjectFind(g_chart_id, name) < 0)
      if(!ObjectCreate(g_chart_id, name, OBJ_EDIT, 0, 0, 0))
         return false;

   ObjectSetInteger(g_chart_id, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(g_chart_id, name, OBJPROP_XSIZE, w);
   ObjectSetInteger(g_chart_id, name, OBJPROP_YSIZE, h);
   ObjectSetInteger(g_chart_id, name, OBJPROP_BGCOLOR, clrWhite);
   ObjectSetInteger(g_chart_id, name, OBJPROP_COLOR, clrBlack);
   ObjectSetInteger(g_chart_id, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(g_chart_id, name, OBJPROP_FONT, "Arial");
   ObjectSetString(g_chart_id, name, OBJPROP_TEXT, txt);
   ObjectSetInteger(g_chart_id, name, OBJPROP_HIDDEN, true);
   return true;
}

void UISetText(const string name, const string txt)
{
   if(ObjectFind(g_chart_id, name) >= 0)
      ObjectSetString(g_chart_id, name, OBJPROP_TEXT, txt);
}

string UIGetText(const string name)
{
   if(ObjectFind(g_chart_id, name) < 0)
      return "";
   return ObjectGetString(g_chart_id, name, OBJPROP_TEXT);
}

void UIDeleteAll()
{
   string names[] =
   {
      UI+"BG",UI+"HEADER",UI+"TITLE",UI+"SUB",UI+"BTN_TOGGLE",UI+"MINI",
      UI+"LAB_STATUS1",UI+"LAB_STATUS2",
      UI+"SEC1",UI+"SEC2",UI+"SEC3",UI+"SEC4",
      UI+"SEP1",UI+"SEP2",UI+"SEP3",
      UI+"LAB_ROLE",UI+"BTN_ROLE",
      UI+"LAB_API",UI+"ED_API",
      UI+"LAB_TOKEN",UI+"ED_TOKEN",
      UI+"LAB_LICENSE",UI+"ED_LICENSE",
      UI+"LAB_TIMER",UI+"ED_TIMER",
      UI+"BTN_RUN",UI+"BTN_ACTIVATE",
      UI+"LAB_MAP",UI+"ED_MAP",UI+"BTN_DIRECT",
      UI+"LAB_LOTM",UI+"ED_LOTM",
      UI+"LAB_FIXLOT",UI+"ED_FIXLOT",
      UI+"BTN_FIXED",UI+"BTN_SLTP",
      UI+"BTN_APPLY",UI+"BTN_PUB",UI+"BTN_SYNC"
   };

   for(int i = 0; i < ArraySize(names); i++)
      DeleteIfExists(names[i]);
}

void UICreate()
{
   UIDeleteAll();

   color header_color = (g_role == ROLE_MASTER ? clrDodgerBlue : clrForestGreen);

   if(!g_panel_expanded)
   {
      CreateRect(UI+"BG", PANEL_X, PANEL_Y, PANEL_W_MIN, PANEL_H_MIN, clrWhite, clrSilver);
      CreateRect(UI+"HEADER", PANEL_X, PANEL_Y, PANEL_W_MIN, PANEL_H_MIN, header_color, header_color);
      CreateLabel(UI+"TITLE", PANEL_X + 10, PANEL_Y + 7, "RCX WEB COPIER", 11, clrWhite, true);
      CreateButton(UI+"BTN_TOGGLE", PANEL_X + PANEL_W_MIN - 28, PANEL_Y + 9, 18, 18, "+", clrWhite, clrBlack);
      CreateLabel(UI+"MINI", PANEL_X + 145, PANEL_Y + 12, "", 8, clrWhite, false);
      return;
   }

   CreateRect(UI+"BG", PANEL_X, PANEL_Y, PANEL_W, PANEL_H, clrWhite, clrSilver);
   CreateRect(UI+"HEADER", PANEL_X, PANEL_Y, PANEL_W, 76, header_color, header_color);
   CreateLabel(UI+"TITLE", PANEL_X + 14, PANEL_Y + 16, "RCX WEB COPIER", 18, clrWhite, true);
   CreateLabel(UI+"SUB", PANEL_X + 14, PANEL_Y + 42, "Panel + Symbol Mapping + API Sync", 10, clrWhite, false);
   CreateButton(UI+"BTN_TOGGLE", PANEL_X + PANEL_W - 32, PANEL_Y + 12, 20, 20, "-", clrWhite, clrBlack);

   CreateLabel(UI+"LAB_STATUS1", PANEL_X + 16, PANEL_Y + 88, "", 11, clrBlack, true);
   CreateLabel(UI+"LAB_STATUS2", PANEL_X + 16, PANEL_Y + 108, "", 9, clrDimGray, false);

   CreateLabel(UI+"SEC1", PANEL_X + 16, PANEL_Y + 140, "Connection", 11, clrBlack, true);
   CreateRect(UI+"SEP1", PANEL_X + 16, PANEL_Y + 160, PANEL_W - 32, 1, clrSilver, clrSilver);

   CreateLabel(UI+"LAB_ROLE", PANEL_X + 18, PANEL_Y + 176, "Role", 9, clrBlack, false);
   CreateButton(UI+"BTN_ROLE", PANEL_X + 100, PANEL_Y + 170, 110, 24, "", C'235,235,235', clrBlack);

   CreateLabel(UI+"LAB_API", PANEL_X + 18, PANEL_Y + 208, "API URL", 9, clrBlack, false);
   CreateEdit(UI+"ED_API", PANEL_X + 100, PANEL_Y + 202, 320, 24, "");

   CreateLabel(UI+"LAB_TOKEN", PANEL_X + 18, PANEL_Y + 240, "Master Token", 9, clrBlack, false);
   CreateEdit(UI+"ED_TOKEN", PANEL_X + 100, PANEL_Y + 234, 150, 24, "");

   CreateLabel(UI+"LAB_LICENSE", PANEL_X + 266, PANEL_Y + 240, "License", 9, clrBlack, false);
   CreateEdit(UI+"ED_LICENSE", PANEL_X + 322, PANEL_Y + 234, 150, 24, "");

   CreateLabel(UI+"LAB_TIMER", PANEL_X + 18, PANEL_Y + 272, "Timer ms", 9, clrBlack, false);
   CreateEdit(UI+"ED_TIMER", PANEL_X + 100, PANEL_Y + 266, 80, 24, "");

   CreateButton(UI+"BTN_RUN", PANEL_X + 196, PANEL_Y + 266, 110, 24, "", clrForestGreen, clrWhite);
   CreateButton(UI+"BTN_ACTIVATE", PANEL_X + 322, PANEL_Y + 266, 110, 24, "ACTIVATE", clrDarkOrange, clrWhite);

   CreateLabel(UI+"SEC2", PANEL_X + 16, PANEL_Y + 308, "Symbol Mapping", 11, clrBlack, true);
   CreateRect(UI+"SEP2", PANEL_X + 16, PANEL_Y + 328, PANEL_W - 32, 1, clrSilver, clrSilver);

   CreateLabel(UI+"LAB_MAP", PANEL_X + 18, PANEL_Y + 344, "Map", 9, clrBlack, false);
   CreateEdit(UI+"ED_MAP", PANEL_X + 100, PANEL_Y + 338, 372, 24, "");
   CreateButton(UI+"BTN_DIRECT", PANEL_X + 482, PANEL_Y + 338, 100, 24, "", C'235,235,235', clrBlack);

   CreateLabel(UI+"SEC3", PANEL_X + 16, PANEL_Y + 382, "Risk / Options", 11, clrBlack, true);
   CreateRect(UI+"SEP3", PANEL_X + 16, PANEL_Y + 402, PANEL_W - 32, 1, clrSilver, clrSilver);

   CreateLabel(UI+"LAB_LOTM", PANEL_X + 18, PANEL_Y + 418, "Lot Mult", 9, clrBlack, false);
   CreateEdit(UI+"ED_LOTM", PANEL_X + 100, PANEL_Y + 412, 90, 24, "");

   CreateLabel(UI+"LAB_FIXLOT", PANEL_X + 208, PANEL_Y + 418, "Fixed Lot", 9, clrBlack, false);
   CreateEdit(UI+"ED_FIXLOT", PANEL_X + 278, PANEL_Y + 412, 90, 24, "");

   CreateButton(UI+"BTN_FIXED", PANEL_X + 388, PANEL_Y + 412, 90, 24, "", C'235,235,235', clrBlack);
   CreateButton(UI+"BTN_SLTP", PANEL_X + 490, PANEL_Y + 412, 92, 24, "", C'235,235,235', clrBlack);

   CreateLabel(UI+"SEC4", PANEL_X + 16, PANEL_Y + 456, "Actions", 11, clrBlack, true);

   CreateButton(UI+"BTN_APPLY", PANEL_X + 18, PANEL_Y + 486, 84, 26, "APPLY", clrDodgerBlue, clrWhite);
   CreateButton(UI+"BTN_PUB", PANEL_X + 116, PANEL_Y + 486, 84, 26, "PUB", clrDarkOrange, clrWhite);
   CreateButton(UI+"BTN_SYNC", PANEL_X + 214, PANEL_Y + 486, 84, 26, "SYNC", clrDarkOrange, clrWhite);
}

void UISyncFromRuntime()
{
   if(!g_panel_expanded)
      return;

   UISetText(UI+"ED_API", g_api_base);
   UISetText(UI+"ED_TOKEN", g_master_token);
   UISetText(UI+"ED_LICENSE", g_slave_license_key);
   UISetText(UI+"ED_TIMER", IntegerToString(g_timer_ms));
   UISetText(UI+"ED_MAP", g_symbol_map_csv);
   UISetText(UI+"ED_LOTM", DoubleToString(g_lot_multiplier, 2));
   UISetText(UI+"ED_FIXLOT", DoubleToString(g_fixed_lot, 2));
}

void UIUpdate()
{
   color header_color = (g_role == ROLE_MASTER ? clrDodgerBlue : clrForestGreen);

   if(ObjectFind(g_chart_id, UI+"HEADER") >= 0)
   {
      ObjectSetInteger(g_chart_id, UI+"HEADER", OBJPROP_BGCOLOR, header_color);
      ObjectSetInteger(g_chart_id, UI+"HEADER", OBJPROP_COLOR, header_color);
   }

   string run_state = (g_enabled ? "RUNNING" : "STOPPED");

   if(!g_panel_expanded)
   {
      UISetText(UI+"MINI",
                RoleText(g_role) + " | " +
                AccountModeText() + " | " +
                run_state + " | PUB " + TimeText(g_last_publish_time) +
                " | SYNC " + TimeText(g_last_sync_time));
      ChartRedraw();
      return;
   }

   UISetText(UI+"LAB_STATUS1", RoleText(g_role) + " | " + AccountModeText() + " | " + run_state);
   UISetText(UI+"LAB_STATUS2", "Publish: " + TimeText(g_last_publish_time) +
                               " | Sync: " + TimeText(g_last_sync_time) +
                               " | Activated: " + (g_slave_activated ? "YES" : "NO"));

   UISetText(UI+"BTN_ROLE", RoleText(g_role));
   UISetText(UI+"BTN_RUN", g_enabled ? "RUNNING" : "STOPPED");
   UISetText(UI+"BTN_DIRECT", "DIRECT " + BoolText(g_use_direct_match));
   UISetText(UI+"BTN_FIXED", "FIXED " + BoolText(g_use_fixed_lot));
   UISetText(UI+"BTN_SLTP", "SLTP " + BoolText(g_copy_sltp));

   if(ObjectFind(g_chart_id, UI+"BTN_RUN") >= 0)
      ObjectSetInteger(g_chart_id, UI+"BTN_RUN", OBJPROP_BGCOLOR, (g_enabled ? clrForestGreen : clrFireBrick));

   ChartRedraw();
}

void RebuildUI()
{
   UICreate();
   UISyncFromRuntime();
   UIUpdate();
}

//====================================================
// Runtime apply
//====================================================
void ApplyInputsToRuntime()
{
   g_role                 = InpRole;
   g_api_base             = InpApiBase;
   g_master_token         = InpMasterToken;
   g_slave_license_key    = InpSlaveLicenseKey;
   g_use_timer            = InpUseTimer;
   g_timer_ms             = InpTimerMs;
   g_lot_multiplier       = InpLotMultiplier;
   g_use_fixed_lot        = InpUseFixedLot;
   g_fixed_lot            = InpFixedLot;
   g_magic                = InpMagic;
   g_max_deviation_points = InpMaxDeviationPoints;
   g_copy_sltp            = InpCopySLTP;
   g_allow_symbols_csv    = InpAllowSymbolsCsv;
   g_deny_symbols_csv     = InpDenySymbolsCsv;
   g_symbol_map_csv       = InpSymbolMapCsv;
   g_use_direct_match     = InpUseDirectMatch;
   g_comment_prefix       = InpCommentPrefix;
}

void RestartTimer()
{
   EventKillTimer();
   if(g_use_timer)
      EventSetMillisecondTimer(MathMax(g_timer_ms, 100));
}

void ApplyPanelToRuntime()
{
   if(!g_panel_expanded)
      return;

   g_api_base          = Trim(UIGetText(UI+"ED_API"));
   g_master_token      = Trim(UIGetText(UI+"ED_TOKEN"));
   g_slave_license_key = Trim(UIGetText(UI+"ED_LICENSE"));
   g_timer_ms          = (int)StringToInteger(Trim(UIGetText(UI+"ED_TIMER")));
   g_symbol_map_csv    = Trim(UIGetText(UI+"ED_MAP"));
   g_lot_multiplier    = StringToDouble(Trim(UIGetText(UI+"ED_LOTM")));
   g_fixed_lot         = StringToDouble(Trim(UIGetText(UI+"ED_FIXLOT")));

   if(g_timer_ms < 100) g_timer_ms = 100;
   if(g_lot_multiplier <= 0.0) g_lot_multiplier = 1.0;
   if(g_fixed_lot <= 0.0) g_fixed_lot = 0.01;

   RestartTimer();
   UIUpdate();
   Print("Panel settings applied.");
}

//====================================================
// Events
//====================================================
int OnInit()
{
   g_chart_id = ChartID();

   ApplyInputsToRuntime();
   DetectAccountMode();

   trade.SetAsyncMode(false);
   trade.SetDeviationInPoints(g_max_deviation_points);

   RestartTimer();
   RebuildUI();

   Print("RCX Web Panel init. role=", RoleText(g_role),
         " mode=", AccountModeText(),
         " api=", g_api_base,
         " map=", g_symbol_map_csv);

   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   UIDeleteAll();
}

void OnTick()
{
   if(!g_use_timer)
   {
      if(g_role == ROLE_MASTER) PublishSnapshot();
      else                      SlaveSync();
   }

   UIUpdate();
}

void OnTimer()
{
   if(g_role == ROLE_MASTER) PublishSnapshot();
   else                      SlaveSync();

   UIUpdate();
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(g_role == ROLE_MASTER && g_enabled)
      PublishSnapshot();

   UIUpdate();
}

void OnChartEvent(const int id,
                  const long &lparam,
                  const double &dparam,
                  const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_CLICK)
      return;

   if(sparam == UI+"BTN_TOGGLE")
   {
      g_panel_expanded = !g_panel_expanded;
      RebuildUI();
      return;
   }

   if(sparam == UI+"BTN_ROLE")
   {
      g_role = (g_role == ROLE_MASTER ? ROLE_SLAVE : ROLE_MASTER);
      UIUpdate();
   }
   else if(sparam == UI+"BTN_RUN")
   {
      g_enabled = !g_enabled;
      UIUpdate();
   }
   else if(sparam == UI+"BTN_DIRECT")
   {
      g_use_direct_match = !g_use_direct_match;
      UIUpdate();
   }
   else if(sparam == UI+"BTN_FIXED")
   {
      g_use_fixed_lot = !g_use_fixed_lot;
      UIUpdate();
   }
   else if(sparam == UI+"BTN_SLTP")
   {
      g_copy_sltp = !g_copy_sltp;
      UIUpdate();
   }
   else if(sparam == UI+"BTN_ACTIVATE")
   {
      ApplyPanelToRuntime();
      g_slave_activated = false;
      ActivateSlaveOnline();
      UIUpdate();
   }
   else if(sparam == UI+"BTN_APPLY")
   {
      ApplyPanelToRuntime();
   }
   else if(sparam == UI+"BTN_PUB")
   {
      ApplyPanelToRuntime();
      if(g_role == ROLE_MASTER)
         PublishSnapshot();
      else
         Print("PUB is only for MASTER.");
   }
   else if(sparam == UI+"BTN_SYNC")
   {
      ApplyPanelToRuntime();
      if(g_role == ROLE_SLAVE)
         SlaveSync();
      else
         Print("SYNC is only for SLAVE.");
   }
}