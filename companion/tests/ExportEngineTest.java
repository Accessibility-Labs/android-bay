package org.androidrescue.helper;

import android.database.Cursor;
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.*;
import org.json.*;

/** Host tests exercise real exporter code against deterministic read-only provider fixtures. */
public final class ExportEngineTest {
    static int checks;
    static final String CONTACTS = "content://com.android.contacts/contacts";
    static void check(boolean condition, String message) {
        checks++;
        if (!condition) throw new AssertionError(message);
    }
    static LinkedHashMap<String,Object> row(Object... cells) {
        LinkedHashMap<String,Object> result = new LinkedHashMap<String,Object>();
        for (int i=0; i<cells.length; i+=2) result.put((String) cells[i], cells[i+1]);
        return result;
    }
    static final class FakeProvider implements ExportEngine.Provider {
        Map<String,List<LinkedHashMap<String,Object>>> rows = new HashMap<String,List<LinkedHashMap<String,Object>>>();
        Map<String,byte[]> files = new HashMap<String,byte[]>();
        Set<String> denied = new HashSet<String>();
        Set<String> nullCursors = new HashSet<String>();
        Set<String> brokenStreams = new HashSet<String>();
        Set<String> queries = new HashSet<String>();
        int closed;
        File output;
        void put(String uri, LinkedHashMap<String,Object>... records) { rows.put(uri, Arrays.asList(records)); }
        public Cursor query(String uri) throws Exception {
            queries.add(uri);
            check(!new File(output, "report.json").exists(), "Final report must not exist before all reads finish");
            if (denied.contains(uri)) throw new SecurityException("fixture permission denied");
            if (nullCursors.contains(uri)) return null;
            final List<LinkedHashMap<String,Object>> records = rows.containsKey(uri) ? rows.get(uri) : Collections.<LinkedHashMap<String,Object>>emptyList();
            final String[] columns = records.isEmpty() ? new String[]{"_id"} : records.get(0).keySet().toArray(new String[0]);
            return (Cursor) Proxy.newProxyInstance(Cursor.class.getClassLoader(), new Class<?>[]{Cursor.class}, new InvocationHandler() {
                int position=-1;
                public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                    String name = method.getName();
                    if (name.equals("getCount")) return records.size();
                    if (name.equals("getColumnNames")) return columns;
                    if (name.equals("moveToNext")) return ++position < records.size();
                    if (name.equals("close")) { closed++; return null; }
                    if (name.equals("toString")) return "FixtureCursor";
                    Object value = records.get(position).get(columns[(Integer) args[0]]);
                    if (value instanceof IOException) throw (IOException)value;
                    if (name.equals("getType")) {
                        if (value == null) return Cursor.FIELD_TYPE_NULL;
                        if (value instanceof byte[]) return Cursor.FIELD_TYPE_BLOB;
                        if (value instanceof Float || value instanceof Double) return Cursor.FIELD_TYPE_FLOAT;
                        if (value instanceof Number) return Cursor.FIELD_TYPE_INTEGER;
                        return Cursor.FIELD_TYPE_STRING;
                    }
                    if (name.equals("getLong")) return ((Number)value).longValue();
                    if (name.equals("getDouble")) return ((Number)value).doubleValue();
                    if (name.equals("getString")) return value.toString();
                    if (name.equals("getBlob")) return value;
                    throw new AssertionError("Unexpected cursor operation " + name);
                }
            });
        }
        public InputStream open(String uri) throws Exception {
            check(!new File(output, "report.json").exists(), "Report must follow attachment writes");
            if (brokenStreams.contains(uri)) return new InputStream() {
                int reads;
                public int read() throws IOException { if (reads++ < 4) return 97; throw new IOException("fixture interrupted stream"); }
            };
            if (!files.containsKey(uri)) throw new FileNotFoundException(uri);
            return new ByteArrayInputStream(files.get(uri));
        }
        public String base64(byte[] value) { return Base64.getEncoder().encodeToString(value); }
    }
    static JSONObject run(FakeProvider provider, Path parent, String name) throws Exception {
        provider.output = parent.resolve(name).toFile();
        return new ExportEngine(provider, new ExportEngine.Progress() { public void update(String message) {} }, provider.output, new JSONObject().put("fixture", true)).run();
    }
    static JSONObject first(File dir, String file) throws Exception {
        return new JSONObject(Files.readAllLines(new File(dir,file).toPath(), StandardCharsets.UTF_8).get(0));
    }
    static byte[] bytes(String value) { return value.getBytes(StandardCharsets.UTF_8); }
    public static void main(String[] args) throws Exception {
        Path parent = Files.createTempDirectory("android-rescue-helper-test-");
        FakeProvider good = new FakeProvider();
        good.put(CONTACTS, row("_id",1L,"lookup","name /&","photo_uri","content://contact/photo"));
        good.files.put(CONTACTS + "/as_vcard/name%20%2F%26",bytes("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Test\r\nEND:VCARD\r\n"));
        good.files.put("content://contact/photo",new byte[]{1,2,3,0,(byte)255});
        good.put("content://com.android.contacts/data", row("_id",4L,"data1","Unicode café 雪\nline","data2",null,"data15",new byte[]{0,1,2,(byte)255},"float",1.25,"large",Long.MAX_VALUE));
        good.put("content://mms",row("_id",7L,"date",1700000000L));
        good.put("content://mms/7/addr",row("_id",8L,"msg_id",7L,"type",137L,"address","+15551234567"));
        good.put("content://mms/part",row("_id",10L,"mid",7L,"ct","image/png","_data","/private/provider/path","text",null),row("_id",11L,"mid",7L,"ct","text/plain","_data",null,"text","MMS body"));
        good.files.put("content://mms/part/10",new byte[]{(byte)137,80,78,71,0,1,2});
        good.put("content://com.android.calendar/events",row("_id",12L,"calendar_id",1L,"rrule","FREQ=MONTHLY;BYDAY=MO","exdate","20251005T000000Z"));
        JSONObject result = run(good,parent,"good");
        check(result.getBoolean("runFinished") && result.getBoolean("complete"),"Happy run finishes completely");
        check(result.getJSONArray("errors").length()==0,"Happy run has no errors");
        check(result.getLong("exportedBinaryFiles")==3,"vCard, photo and MMS binary counted");
        check(new File(good.output,"report.json").isFile(),"Final report exists");
        check(good.closed==good.queries.size(),"All cursors closed");
        JSONObject data = first(good.output,"contact_data.jsonl").getJSONObject("values");
        check(data.isNull("data2"),"SQL null retained");
        check(data.getLong("large")==Long.MAX_VALUE,"64-bit integer retained");
        check(data.getDouble("float")==1.25,"Floating point retained");
        check(data.getString("data1").equals("Unicode café 雪\nline"),"Unicode/newline retained");
        check(data.getJSONObject("data15").getString("$blob").equals("AAEC/w=="),"Blob lossless base64");
        check(first(good.output,"mms-addresses/message-7.jsonl").getJSONObject("values").getLong("msg_id")==7,"MMS relationship retained");
        JSONObject attachment = first(good.output,"mms_parts.jsonl").getJSONObject("export").getJSONObject("attachment");
        check(attachment.getLong("bytes")==7,"Attachment byte count recorded");
        check(attachment.getString("sha256").length()==64,"Attachment hash recorded");
        check(Arrays.equals(Files.readAllBytes(new File(good.output,"mms-attachments/part-10.bin").toPath()),good.files.get("content://mms/part/10")),"Binary attachment exact");
        check(first(good.output,"calendar_events.jsonl").getJSONObject("values").getString("rrule").startsWith("FREQ="),"Calendar recurrence retained");
        check(Files.readAllLines(new File(good.output,"mms_parts.jsonl").toPath(), StandardCharsets.UTF_8).get(1).contains("MMS body"),"Inline MMS text retained");

        FakeProvider partial = new FakeProvider();
        partial.denied.add("content://com.android.calendar/events");
        partial.nullCursors.add("content://call_log/calls");
        partial.put(CONTACTS,row("_id",1L,"lookup","missing-vcard","photo_uri","content://contact/photo"));
        partial.files.put("content://contact/photo",bytes("photo still exports"));
        partial.put("content://com.android.contacts/data",row("_id",2L,"broken",new IOException("unreadable field"),"good","retained"));
        partial.put("content://mms/part",row("_id",10L,"mid",7L,"ct","image/png","_data","/private/path"));
        partial.brokenStreams.add("content://mms/part/10");
        JSONObject failed = run(partial,parent,"partial");
        check(failed.getString("status").equals("partial") && !failed.getBoolean("complete"),"Errors cannot produce complete status");
        check(failed.getJSONArray("errors").length()==5,"Denial, null cursor, vcard, field, stream errors all retained");
        check(new File(partial.output,"contact-photos/contact-1.bin").isFile(),"Photo still exported when vcard fails");
        check(!new File(partial.output,"mms-attachments/part-10.bin").exists(),"Failed attachment never finalized");
        check(new File(partial.output,"mms-attachments/part-10.bin.partial").isFile(),"Failed attachment partial retained");
        check(first(partial.output,"contact_data.jsonl").getJSONObject("values").getJSONObject("broken").has("$error"),"Unreadable field distinguishable from null");
        check(first(partial.output,"contact_data.jsonl").getJSONObject("values").getString("good").equals("retained"),"Other fields survive individual field error");
        check(partial.queries.contains("content://com.android.calendar/extendedproperties"),"Later categories still attempted after errors");

        FakeProvider attack = new FakeProvider();
        attack.put("content://mms/part",row("_id","../../escape","mid",7L,"ct","image/png","_data","/private/path"));
        JSONObject rejected = run(attack,parent,"invalid-id");
        check(rejected.getJSONArray("errors").length()==1,"Invalid provider ID is rejected");
        check(!new File(parent.toFile(),"escape.bin").exists(),"Provider ID cannot escape output directory");
        boolean refused = false;
        try { new ExportEngine(good,new ExportEngine.Progress(){public void update(String s){}},good.output,new JSONObject()).run(); }
        catch(IOException expected) { refused=true; }
        check(refused,"Existing export must never be overwritten");
        System.out.println("PASS: " + checks + " exporter assertions. Fixtures: " + parent);
    }
}
