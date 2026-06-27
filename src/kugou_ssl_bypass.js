console.log("[*] 开始注入...");

// 暴力尝试 Hook 所有可能的 SSL 验证函数
var bypassed = false;

// 1. 尝试 Hook WinINet
try {
    var wininet = Module.load("wininet.dll");
    var InternetSetOptionW = wininet.getExportByName("InternetSetOptionW");
    
    Interceptor.attach(InternetSetOptionW, {
        onEnter: function(args) {
            if (args[1].toInt32() == 31) {
                var ptr = args[2];
                var flags = ptr.readU32();
                // 0x2000 | 0x1000 | 0x800 = 忽略所有证书错误
                ptr.writeU32(flags | 0x00003800);
            }
        }
    });
    console.log("[+] WinINet 绕过成功");
    bypassed = true;
} catch(e) {
    console.log("[-] WinINet 未加载");
}

// 2. 尝试 Hook WinHTTP
try {
    var winhttp = Module.load("winhttp.dll");
    var WinHttpSetOption = winhttp.getExportByName("WinHttpSetOption");
    
    Interceptor.attach(WinHttpSetOption, {
        onEnter: function(args) {
            if (args[1].toInt32() == 31) {
                var ptr = args[2];
                var flags = ptr.readU32();
                ptr.writeU32(flags | 0x00003800);
            }
        }
    });
    console.log("[+] WinHTTP 绕过成功");
    bypassed = true;
} catch(e) {
    console.log("[-] WinHTTP 未加载");
}

// 3. 尝试 OpenSSL 通用 Hook
try {
    // 遍历常见的 libssl 名称
    var ssl_names = ["libssl.dll", "libssl-1_1.dll", "ssleay32.dll"];
    for (var i = 0; i < ssl_names.length; i++) {
        try {
            var lib = Module.load(ssl_names[i]);
            var SSL_get_verify_result = lib.getExportByName("SSL_get_verify_result");
            
            Interceptor.attach(SSL_get_verify_result, {
                onLeave: function(ret) {
                    ret.replace(0); // 0 表示验证通过
                }
            });
            console.log("[+] OpenSSL (" + ssl_names[i] + ") 绕过成功");
            bypassed = true;
            break;
        } catch(e) {}
    }
} catch(e) {}

if (bypassed) {
    console.log("[*] === 脚本加载完成，SSL 绕过已激活 ===");
} else {
    console.log("[!] 警告：未找到任何网络库，可能需要手动查找");
}