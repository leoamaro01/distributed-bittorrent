# Tecnologías principales


## Sistema de requests
Para la comunicación entre clientes y servidores se creó un conjunto de objetos que cuentan con un mecanismo de serialización y deserialización a bytes nativo, lo cual permite utilizar la comodidad de la programación orientada a objetos sin sacrificar ancho de banda ni rendimiento. 

Con este mismo sistema se transamiten tanto queries sencillas como piezas enteras del torrent.


## Descargas por piezas
Cuando un usuario sube un torrent lo que hace es enviar al servidor al que se encuentre conectado la estructura de directorios del torrent y los datos de cada archivo, estos datos son:
- Nombre completo del archivo, incluyendo los directorios en el camino a este
- Peso del archivo en bytes
- Hashes de verificación para cada pieza del archivo (usamos piezas de 256KB pero es fácilmente modificable sin perder compatibilidad con torrents existentes, ya que el tamaño de las piezas se incluye en la información del torrent que el usuario sube al servidor)

A la hora de descargar las piezas de archivos, estas se descargan en orden aleatorio, con el objetivo de balancear uniformemente la disponibilidad de piezas en el enjambre de usuarios, y se descargan de otros usuarios aleatorios también, a fin de balancear las descargas entre todos los usuarios que están compartiendo el torrent.

Cada pieza que se descarga se comprueba el hash de sus bytes con el hash especificado en la información del torrent, asegurándonos de que no pueda realizarse manipulación accidental o malintencionada de los archivos del torrent. Entonces los datos (bytes) de la pieza se guardan en un archivo en una carpeta destinada a las descargas parciales.

Una vez se descargan todas las piezas de un archivo, se verifican, y si una o varias piezas fallan la verificación (se hace este paso nuevamente para evitar la manipulación de piezas durante la descarga), se borran las piezas descargadas y se vuelven a agregar a la lista de descargas pendientes; en caso de que pasen la verificación se reconstruye el archivo con los contenidos de los archivos de piezas y una vez queda reconstruido el archivo, se eliminan las piezas de la carpeta de descargas parciales.


## Chord
Se utilizó Chord como sistema de distribución de datos, debido a su demostrado rendimieno y robustez ante fallas.

Los datos distribuidos son la información de los torrents y los usuarios que se encuentran online, de forma tal que el nodo que almacena localmente los usuarios online es el encargado de comprobar qué torrents tiene disponible el usuario.

### Estrategias de resistencia a fallas
Se adicionaron al protocolo de Chord básico algunas estrategias de replicación para hacerlo más resistente a fallos inesperados de nodos del anillo.

- #### Conjunto de sucesores inmediatos
    Todo nodo mantiene, además de su sucesor inmediato, un conjunto de sucesores de longitud $r = 16$, se demuestra en el [artículo de Chord](https://pdos.csail.mit.edu/papers/chord:sigcomm01/chord_sigcomm.pdf) que para una red inicialmente estable con una cantidad de nodos $N$, y una lista de sucesores de longitud $O(log N)$, y entonces todos los nodos fallan con una probabilidad de $1/2$, entonces con alta probabilidad la función `find_successor` devuelve el sucesor vivo más cercano a la llave pedida, lo cuál se cumpliría, en este caso, hasta $2^{16}=65,536$ nodos. Dicho conjunto de sucesores lo utilizaremos también en la replicación de datos.

- #### Replicación de datos
    Cada nodo es responsable por no solamente los torrents y usuarios de cuyas llaves este es sucesor, sino tambien de un conjunto de réplicas de datos de nodos predecesores a este.

    El ciclo de vida de las réplicas es el siguiente:
    1. Cuando un dato se almacena en un nodo, este replica el dato a todos los nodos en su lista de sucesores.
    2. Cuando cambia la lista de sucesores del nodo, se replican todos los datos existentes del nodo a los nuevos y se eliminian las réplicas correspondientes de los nodos que se eliminaron de los sucesores
    3. Cuando un nodo pierde su predecesor (ya sea por falla o cualquier otra razón), toma posesión de los datos que tenía almacenados como réplica de los datos de su predecesor
    4. Cuando un nodo obtiene un nuevo predecesor, transfiere parte de sus datos propios a su nuevo predecesor, para luego insertarse a través de (1) en este mismo nodo pero esta vez en forma de réplica


## Descubrimiento
Tanto clientes como servidores tienen que llevar a cabo estrategias de descubrimiento para conocer nuevos servidores en el caso en que fallen con los que se han conectado hasta el momento. Ya se habló de una forma en la que los servidores logran esto: la lista de sucesores, y los clientes hacen algo similar. Los clientes mantienen una lista de "servidores conocidos", y siempre tratan de conectarse al primer elemento de esta lista, si en algún momento fallase la conexión se eliminaría este servidor de la lista de conocidos y se intentaría con el siguiente, y así sucesivamente. Pero, ¿qué ocurre cuando, ya sea un cliene o un servidor, no conoce ningún servidor?

### Multicast
Tanto clientes como servidores utilizan el servicio de multicast para encontrar servidores disponibles en la red local, todos los servidores siempre tienen abierto un puerto en multicast esperando para responder a cualquier cliente o servidor que necesite conectarse a la red

Esto puede ser suficiente para los servidores que utilizan de todas formas los mecanismos de Chord para conocer nuevos servidores constantemente, pero los clientes no poseen ningün mecanismo similar, y si todos los servidores en su red local desaparecen no tendría manera de comunicarse con servidores remotos. En respuesta a esto:

### Word Of Mouth
Cuando los clientes se encuentren en bajo suministro de servidores conocidos (menos de 20 por defecto), le preguntarán tanto a servidores como a otros usuarios si tal vez conocen servidores que el cliente no conoce, esparciendo así conocimiento sobre servidores remotos a clientes locales (o menos remotos en cualquier caso)

# Prerequisitos
## Windows
Debe tener instalado Docker, WSL y make para ejecutar los comandos del makefile (recomendable). Make se puede instalar con Chocolatey con el comando
```
choco install make
```

## GNU/Linux
Debe instalar Docker para poder utilizar el proyecto.

# Uso
## Inicialización
Para ejecutar el proyecto en una red de docker, se debe ejecutar el comando
```
make prerun
```
El cual crea una red de docker a través de la cual se comunicarán nuestros clientes y servidores, y un volumen de docker donde se pueden mantener archivos para fácil prueba de subida de torrents sin tener que copiar archivos repetitivamente a los contenedores

### Servidor
Para ejecutar el servidor se utiliza
```
make redeploy-tracker
```
Esto limpiará ejecuciones anteriores del servidor, reconstruirá la imagen de docker, y levantará un contenedor con esta imagen con el nombre `bittorrent-tracker`

Si levantar más contenedores en la misma máquina (para probar con una red de docker), se utiliza
```
make redeploy-tracker-cluster
```
Este hace lo mismo que el anterior, pero con 5 (personalizable) trackers con el nombre `bittorrent-tracker-i`, donde i será el índice del servidor con respecto a los otros cuatro. Se deben esperar 5 segundos desde que se inicia el primer tracker para iniciar los demás para que puedan conectarse apropiadamente al mismo anillo de Chord, pero una vez que se inicia el primer tracker todos los consiguientes pueden crearse sin espera ninguna.

### Cliente
Para crear un cliente que utilice el volumen de docker que crea el comando de Make `prerun`, se debe utilizar
```
make redeploy-holder-client
```
Para crear un cliente común sin acceso a ese volumen se puede utilizar
```
make redeploy-client
```
Y (nuevamente, para probar en una red de docker, no deben haber varios clientes ni servidores en una misma máquina en un ambiente local) para montar varios clientes a la vez, se puede usar
```
make redeploy-client-cluster
```

## Uso del programa
Tanto el servidor como el cliente cuentan con acceso a una Interfaz de Línea de Comandos (CLI), a la que puede acceder utilizando
```
docker attach (nombre del contenedor)
```
Posee un comando `help` en ambos casos que puede utilizar para conocer las funcionalidades que ofrece la CLI